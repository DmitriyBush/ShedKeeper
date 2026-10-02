"""SessionStart / SessionEnd hook: keeps copies of the session transcripts.

SessionEnd:   copies `transcript_path` to <store>\\transcripts\\<project folder>\\<session_id>.jsonl unless
              a copy at least as long is already there (the copy goes to a temporary name, then one
              os.replace puts it in place).
SessionStart: goes through every *.jsonl of the same project folder and saves what is missing by the
              same rule -- this catches sessions that ended without a SessionEnd (a crash). A time
              ceiling of 20 seconds; what is left waits for the next start.
              ALWAYS prints one short additionalContext (the agent knows from the first minute that the
              kit is installed, how to find and bring back, and that a person's word comes before any
              restore); the warnings (store not usable, hooks not found) follow in the same line.
SessionStart, once per project: when the store has no record that the setup check (`shedkeeper.py survey`) was offered for this project
              (the root of its git repository, else the folder), the same additionalContext carries the offer to the agent -- "once, at a
              natural moment, offer the user, in their language, to run the check; then explain the findings and ask before changing
              anything" -- and the record "offered" is written AT ONCE (shedkeeper/survey.py: an offer is never repeated, answered or not;
              no record can be written -> no offer).
SessionStart, once per USER: the same additionalContext carries the facts about ShedKeeper and its recommendation to the user -- to ask their agent to
              write, in the user's OWN system (the file of instructions their agent reads), a rule about working with ShedKeeper, by three principles
              (shedkeeper/survey.py `rule_offer_text`); the record "rule_offered" is written AT ONCE, in the store, looked up in every store of the user.
              ShedKeeper never writes, reads or checks that file.
SessionStart, while a setting of weight `protection` stands at a value that weakens protection (the shipped default is none): one line
              for the person (`systemMessage`) and one for the agent (the same additionalContext, after the offer) -- what is weakened, the
              key, how to put it back (shedkeeper/settings_cli.py `reminder_lines`). Nothing weakened -> nothing is said.
              A weakening that stands in an ini and acts, but was not made through `set` (config._integrity), carries the mark "not through `set`" in the
              same two places. The prompt hook missing from the settings is a warning too (no confirmation can be received without it).
Both: when the personal folder was made in THIS call, a top-level `systemMessage` carries the report to the
              person (one paragraph: where, closed to whom, why the number, how to switch off).

Two doors: started with `--via-plugin` (the plugin's hooks.json) it does nothing while a live entry of install.py
covers the event (install.plugin_yields: only with CLAUDE_PROJECT_DIR given and disableAllHooks on nowhere), so one handling
per event; the guide then names the plugin way to remove.

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
MAX_OUT = 9500          # the runtime cuts the output of a hook at 10000 characters and then reads NO JSON: the whole answer is lost. Everything the start says stays under this.


def _cut(text, n):
    """`text` cut to at most n characters at a space, with a mark that it was cut."""
    if len(text) <= n:
        return text
    cut = text[:max(0, n - 4)]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n // 2 else cut).rstrip() + " ..."


def _answer(head, warn, offer, remind_agent, remind_person, message, compact_agent, compact_person, kit_py):
    """The JSON object of the start of a session, at most MAX_OUT characters (as the text is written in JSON, `ensure_ascii`: a letter of a path in Cyrillic takes 6).
    What is shortened first is what matters least: the once-only notes (the offer of the check, the recommendation about the rule), then the long text of the
    reminder (a compact one names the keys and where to read the rest), then the warnings of the installation; the line that protection is WEAKENED, with the keys,
    and the guide stay. -> (the object, how many steps of shortening it took)."""
    def build(w, o, ra, rp, m):
        out = {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                      "additionalContext": "shedkeeper: " + head + ((" WARNING: " + w) if w else "") + ((" " + o) if o else "")
                                                           + ((" " + ra) if ra else "")}}
        pm = " ".join(x for x in (m, rp) if x)
        if pm:
            out["systemMessage"] = pm
        return out
    def size(o):
        return len(json.dumps(o, ensure_ascii=True))
    steps = [lambda s: s,
             lambda s: dict(s, ra=compact_agent, rp=compact_person),
             lambda s: dict(s, o=_cut(s["o"], 500)),
             lambda s: dict(s, o=""),
             lambda s: dict(s, w=_cut(s["w"], 2500)),
             lambda s: dict(s, ra=_cut(s["ra"], 1500), rp=_cut(s["rp"], 1500)),
             lambda s: dict(s, m=_cut(s["m"], 1500)),
             lambda s: dict(s, w=_cut(s["w"], 600), ra=_cut(s["ra"], 900), rp=_cut(s["rp"], 900), m=_cut(s["m"], 600))]
    st = {"w": warn, "o": offer, "ra": remind_agent, "rp": remind_person, "m": message}
    out = build(st["w"], st["o"], st["ra"], st["rp"], st["m"])
    n = 0
    for i, step in enumerate(steps[1:], start=1):
        if size(out) <= MAX_OUT:
            break
        st = step(st)
        out = build(st["w"], st["o"], st["ra"], st["rp"], st["m"])
        n = i
    if size(out) > MAX_OUT:             # the last resort: the agent's text is cut at the ceiling, the person's message likewise (never the JSON)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        lo, hi = 0, len(ctx)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            trial = dict(out, hookSpecificOutput=dict(out["hookSpecificOutput"], additionalContext=_cut(ctx, mid)))
            if size(trial) <= MAX_OUT:
                lo = mid
            else:
                hi = mid - 1
        out = dict(out, hookSpecificOutput=dict(out["hookSpecificOutput"], additionalContext=_cut(ctx, lo)))
        n += 1
    return out, n


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


def _warnings(cwd, root, settings, store, problem=None, via_plugin=False):
    """The warnings of the start. `problem` is the reason the store could not be used or made (check_available /
    ensure_store_report said no): it is named here, not re-derived -- a folder that does not exist yet passes
    check_available and still may fail to be created."""
    out = []
    if problem:
        out.append("the store is not usable (%s)" % problem)
    else:
        try:
            if root and os.path.isdir(root) and settings.get("private_store", True):
                private, text = store.private_state(root, settings)
                if not private:
                    out.append("your store %s is NOT closed to other users: %s" % (root, text))
        except Exception as e:
            out.append("could not check whether the store is closed (%s: %s)" % (e.__class__.__name__, e))
    try:
        import install
        if via_plugin:
            pass                        # the hooks are running from the plugin: that is where they are registered
        else:
            found = install.installed_hooks(cwd)
            if not found["pre_tool"]:
                out.append("the pre_tool hook of the kit is not found in the settings (`python install.py install` puts it in; `python install.py check` shows the state)")
            if not found["session"]:
                out.append("the session hook of the kit is not found in the settings")
            if not found["prompt"]:
                out.append("the prompt hook of the kit is not found in the settings: the code a person types to confirm a weakening of protection cannot be received, "
                           "so no such change can be confirmed (`python install.py install` puts it in -- an older install has none; `python install.py check` shows the state)")
            for line in install.stale_entries(cwd):
                installer = os.path.join(KIT, "install.py").replace("\\", "/")
                if "could not be checked" in line:
                    out.append("ShedKeeper could not check its own installation: %s; whether the hook stands in that file is not known "
                               "(`python %s check` says more)" % (line, installer))
                elif "matcher" in line:
                    out.append("the installation of ShedKeeper is of an older form (%s): MCP calls may go unseen and a slow copy may get the hook cut off; "
                               "run `python %s install` (the same folder or project as before)" % (line, installer))
                else:
                    out.append("the hook timeout of ShedKeeper differs from the one this kit ships (%s): a timeout SHORTER than the budgets of the hook can cut a "
                               "slow copy off and then the action goes ahead; a longer one is harmless. If you set it on purpose there is nothing to do; "
                               "otherwise run `python %s install`" % (line, installer))
            plugin = install.plugin_status(os.environ.get("CLAUDE_PROJECT_DIR") or cwd)[0]     # proven: enabled AND installed
            if plugin:
                out.append("ShedKeeper is also enabled as a plugin (%s): its hooks stay silent while this installation "
                           "is live -- keep one (/plugin uninstall %s, or python install.py uninstall)"
                           % (plugin[0][1], install.PLUGIN_ID))
    except Exception as e:
        out.append("could not read the settings (%s)" % e)
    return out


def _guide(usable=True, via_plugin=False):
    """What the agent is told at every start: ONE or two short lines (<= 400 characters for a kit folder path of up to
    ~60 characters, the plugin variant up to ~75: the plugin cache path is long; the commands name the folder once, as
    K). When the store is not usable the line does NOT promise copies. Both end with how to remove ShedKeeper (the
    store stays): the way of the door it came by."""
    k = KIT.replace("\\", "/")
    if via_plugin:
        remove = 'Remove: /plugin uninstall shedkeeper@shedkeeper (or install.py uninstall); the store stays.'
    else:
        remove = ('Remove: python K/install.py uninstall --project <dir> (or --user), then the kit folder to the trash; '
                  'the store stays.')
    what = "ShedKeeper (plugin)" if via_plugin else "ShedKeeper"
    if not usable:
        return ('%s is installed (K="%s") but its store is NOT usable now (see WARNING): no copy is being '
                'saved and edits are refused until it is fixed. Earlier ones: python '
                'K/shedkeeper.py find <part of path>. %s' % (what, k, remove))
    return ('%s is on (K="%s"): a copy is saved before an edit, deleted files go to the trash. Find: python '
            'K/shedkeeper.py find <part of path>; restore <id> puts an earlier version back, with the person\'s agreement. %s' % (what, k, remove))


def _yields_to_install(data):
    """Two doors, one handling: started by the PLUGIN while a live entry of install.py covers this event in the settings
    of the session, this copy stays silent. Any doubt -> False (a doubled hook is untidy, a silent pair is no protection)."""
    try:
        import install
        cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else None
        event = data.get("hook_event_name")
        return event in ("SessionStart", "SessionEnd") and bool(install.plugin_yields(event, cwd))
    except Exception:
        return False


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
    via_plugin = "--via-plugin" in sys.argv[1:]
    if via_plugin and _yields_to_install(data):
        return 0
    root = None
    message = ""
    try:
        from shedkeeper import config, store
        settings, _problems = config.load()
        event = data.get("hook_event_name")
        tp = data.get("transcript_path")
        cwd = data.get("cwd") if isinstance(data.get("cwd"), str) and data.get("cwd") else (
            os.path.dirname(tp) if isinstance(tp, str) and tp else os.getcwd())
        root = store.store_root(cwd, settings)
        # ensure_store_report checks first and refuses with the reason check_available gives; a separate check here
        # would read the labels outside the creation lock, where the first start of a neighbour can fool it
        ok, problem, created = store.ensure_store_report(root, settings)
        if ok:
            try:
                message = store.creation_report(created)
            except Exception as e:      # the person is not told, and the trace stays in the log
                store.log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "report_error",
                                 "error": "%s: %s" % (e.__class__.__name__, e)})
        if ok:
            problem = None
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
        out = {}
        if event != "SessionEnd":
            warn = _warnings(cwd, root, settings, store, problem, via_plugin)
            warn += ["settings: " + str(p)[:300] for p in list(_problems)[:3]]        # a file that cannot be read, a value that is not understood: the safe default acts, and it is said
            remind_person, remind_agent, compact_person, compact_agent = "", "", "", ""
            try:
                from shedkeeper import engines, settings_cli
                remind_person, remind_agent = settings_cli.reminder_lines(settings, KIT.replace("\\", "/") + "/shedkeeper.py")
                compact_person, compact_agent = settings_cli.reminder_lines(settings, KIT.replace("\\", "/") + "/shedkeeper.py", compact=True)
                try:
                    proof_roots = list(dict.fromkeys([root] + list(store.all_roots(settings))))
                except Exception:
                    proof_roots = [root]
                bad = engines.session_notes(proof_roots)
                if bad:                         # an adapter that failed to load: protection of that engine is quietly lower
                    remind_person = (remind_person + " " if remind_person else "") + "ShedKeeper: " + bad + "."
                    remind_agent = (remind_agent + " " if remind_agent else "") + "ShedKeeper warning: " + bad + " (`python K/shedkeeper.py status` shows it)."
                    compact_person = (compact_person + " " if compact_person else "") + "ShedKeeper: " + bad + "."
                    compact_agent = (compact_agent + " " if compact_agent else "") + "ShedKeeper warning: " + bad + " (`python K/shedkeeper.py status` shows it)."
            except Exception as e:      # a reminder is a convenience: its failure is a line in the log, never a failure of the start
                store.log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "reminder_error",
                                 "error": "%s: %s" % (e.__class__.__name__, e)})
            stale = [w for w in warn if "older form" in w or "could not check its own installation" in w or "differs from the one this kit ships" in w or w.startswith("settings: ")]
            if stale:                       # the person is told too: the protection of MCP calls and the time budget are lower than they think
                remind_person = (remind_person + " " if remind_person else "") + "ShedKeeper: " + "; ".join(stale) + "."
                compact_person = (compact_person + " " if compact_person else "") + "ShedKeeper: " + "; ".join(stale) + "."
            offer = ""
            if ok:
                try:
                    from shedkeeper import survey
                    offer = survey.offer_once(root, cwd)
                except Exception as e:      # the offer is a convenience: its failure is a line in the log, never a failure of the start
                    store.log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "survey_offer_error",
                                     "error": "%s: %s" % (e.__class__.__name__, e)})
                try:                        # once per USER: the facts and the recommendation about the rule in the user's own system
                    rule = survey.rule_offer_once(root, store.all_roots(settings))
                    offer = (offer + " " if offer else "") + rule
                except Exception as e:
                    store.log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "rule_offer_error",
                                     "error": "%s: %s" % (e.__class__.__name__, e)})
            out, shortened = _answer(_guide(not problem, via_plugin), "; ".join(warn), offer, remind_agent, remind_person, message,
                                     compact_agent, compact_person, KIT.replace("\\", "/") + "/shedkeeper.py")
            if shortened:
                store.log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "session_start_shortened", "steps": shortened})
        elif message:
            out["systemMessage"] = message
        if out:
            sys.stdout.write(json.dumps(out, ensure_ascii=True))
    except Exception as e:
        try:
            from shedkeeper import store as _s
            _s.log(root, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": "session_hook_error",
                          "error": "%s: %s" % (e.__class__.__name__, e)})
        except Exception:
            pass
        if message:             # a store was made just now: the person is told even when something later failed
            sys.stdout.write(json.dumps({"systemMessage": message}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
