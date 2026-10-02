r"""prove.py -- the proof of an engine adapter (0.3.0).

  python shedkeeper.py prove ENGINE [--target PROBE]

An adapter of the user counts as PROTECTION only while a proof for the CURRENT sha256 of its file is in the store. This command makes the
proof, on a PROBE database -- never on a working one:

  0 the probe is refused unless (a) its name begins with `shedkeeper_probe` (a database / file named so is a probe by the person's own act;
    a working database does not carry that name) and (b) it is EMPTY at the start (the adapter's `probe_state` says so: no table, view,
    sequence, schema). A refusal changes nothing.
  1 fill       a table of known rows (ASCII with the hard characters: quotes, backslash, tab, newline, `%`, `--`, NULLs) is made in the probe,
               and read back through the adapter (the adapter reads what it wrote);
  2 copy       the SAME path the hook and `dbcopy` use (db.copy_base: size, copy, full read-back, the record without a password) takes a copy;
  3 destroy    the table is dropped, and the probe is read again: it must NOT hold the rows any more (a destroy that destroys nothing proves nothing);
  4 restore    the SAME path `restore` uses brings the copy back into a NEW place (a server engine: a new database; a file: the file is put back and
               the one that was there goes to the trash);
  5 compare    the rows of the restored database are compared with the rows that were put in, ROW BY ROW; one difference is a failure.

All five pass -> a line `proved` (engine, sha256 of the adapter file, contract version, time, probe) is written into `proofs.jsonl` of the store;
any step fails -> a line `prove_failed` (it also takes away an earlier proof of the same file: the newest word wins). Nothing is deleted:
what the proof leaves behind (the restored database of a server engine, the old probe file in the trash) is named in the output.

The built-in adapters (PostgreSQL, SQLite) are proved by the tests of the kit: they run THIS function on a SQLite file always, and on a real
PostgreSQL server when the stand is given (SHEDKEEPER_TEST_PG). That is honest because it is the same five steps over the same code the hook
runs, before every release; and a person can run `prove postgres` / `prove sqlite` too. It is not a proof about the person's machine for
PostgreSQL: a PostgreSQL that the tests never saw is covered only by this command.
"""
import os
import time
from datetime import datetime

from . import config, confirm, db, dbcli, engines, store


def probe_rows(n=120):
    """The known rows: (id, text, number or None). ASCII only (a probe database may be in any encoding), hard for every quoting."""
    hard = ["plain text", "it's quoted", 'say "hi"', "back\\slash", "tab\there", "line one\nline two", "semi;colon", "percent %s %d %%",
            "dash -- not a comment", "braces {x} [y] (z)", "trailing space ", "  leading space", "<tag>&amp;</tag>", "hash # sign", "dollar $1 $$"]
    return [(i, "row %d: %s" % (i, hard[i % len(hard)]), None if i % 5 == 0 else i * 7 - 3) for i in range(1, n + 1)]


def diff_rows(want, got):
    """[text] -- the rows that differ, row by row (at most 5 are told), '' lines are not returned: an empty list is an exact match."""
    out = []
    wi, gi = {r[0]: r for r in want}, {r[0]: r for r in got}
    for k in sorted(set(wi) | set(gi)):
        if wi.get(k) != gi.get(k):
            out.append("row id %s: put in %r, came back %r" % (k, wi.get(k), gi.get(k)))
    if len(want) != len(got):
        out.insert(0, "%d row(s) put in, %d came back" % (len(want), len(got)))
    return out[:5] + (["... and %d more" % (len(out) - 5)] if len(out) > 5 else [])


class _Report(object):
    def __init__(self, title):
        self.lines = [title]
        self.ok = True

    def step(self, n, name, ok, detail=""):
        self.lines.append("  [%s] %s %s%s" % ("ok" if ok else "FAILED", n, name, (" -- " + detail) if detail else ""))
        if not ok:
            self.ok = False
        return ok

    def text(self):
        return "\n".join(self.lines)


def _probe_base(ad, spec, cwd):
    """The probe as a db.Base, named THROUGH the adapter even when it is not proven (that is what the proof is for)."""
    if ad.builtin:
        return db.target_from_spec(spec, cwd)
    return ad.name({"target": spec, "source": "prove", "reasons": []}, cwd, dict(os.environ))


def _default_probe(ad, cwd):
    stamp = time.strftime("%Y%m%d_%H%M%S")
    folder = os.path.join(os.path.abspath(cwd or os.getcwd()), "shedkeeper_probe_" + stamp)
    return os.path.join(folder, "shedkeeper_probe_%s%s" % (stamp, (ad.exts[0] if ad.exts else ad.ext)))


def run(engine_id, target_spec, cwd, settings, session="", now=None):
    """-> (exit code, text). 0 proved; 1 refused or not proved."""
    ad = engines.get(engine_id)
    if ad is None:
        broke = engines.broken_claims(engine_id)
        if broke:
            return 1, "REFUSED the adapter file %s failed to load: %s" % (broke[0].path, broke[0].error)
        return 1, ("REFUSED no engine %r: the built-in ones are postgres and sqlite; an adapter is a .py file in %s (see `status`)"
                   % (engine_id, engines.adapters_dir()))
    rep = _Report("prove %s (%s)" % (ad.id, "built in" if ad.builtin else "adapter %s, sha256 %s" % (os.path.basename(ad.path), ad.sha[:12])))
    if not target_spec:
        if not ad.file_based:
            return 1, ("REFUSED give the probe: --target <address of an EMPTY database whose name begins with %s>. A server engine needs a "
                       "database that you made for this; ShedKeeper never makes one on a server." % engines.PROBE_PREFIX)
        target_spec = _default_probe(ad, cwd)
        rep.lines.append("  no --target: a probe file is made at %s (the tool deletes nothing: it and what the proof leaves are yours to remove)" % target_spec)
    try:
        engines.use_proofs([])
        base = _probe_base(ad, target_spec, cwd)
    except Exception as e:
        return 1, "REFUSED the adapter could not name the probe (%s: %s)" % (e.__class__.__name__, db.mask_secrets(str(e))[:200])
    if not isinstance(base, db.Base) or base.engine != ad.id:
        return 1, "REFUSED %r does not name a database of the engine %s (%s)" % (db.mask_secrets(target_spec)[:100], ad.id,
                                                                              getattr(base, "reason", "another engine"))
    if base.linked:
        return 1, "REFUSED a hosted project is not a probe"
    if not base.label.lower().startswith(engines.PROBE_PREFIX):
        return 1, ("REFUSED %s is not marked as a probe: the name of the database (the file) must BEGIN with `%s`. Make an EMPTY database "
                   "with such a name for this proof; a database that holds your work is never used. Nothing was changed."
                   % (base.url_masked, engines.PROBE_PREFIX))
    tools, why = ad.prepare(settings)
    if tools is None:
        return 1, "REFUSED the tools of the engine are not found: %s. Nothing was changed." % why
    try:
        empty, why = ad.probe_state(base, settings, tools)
    except Exception as e:
        return 1, "REFUSED the probe could not be looked at (%s: %s). Nothing was changed." % (e.__class__.__name__, db.mask_secrets(str(e))[:200])
    if not empty:
        return 1, ("REFUSED the probe %s is not empty (%s). A proof destroys what is in the probe, so it runs only on an EMPTY one. Nothing was "
                   "changed." % (base.url_masked, why or "it holds something"))
    root = store.store_root(cwd, settings)
    ok, note, _created = store.ensure_store_report(root, settings)
    if not ok:
        return 1, "REFUSED the store is not usable, so no copy can be made: %s" % note
    rep.lines.append("  probe %s: empty, marked by its name" % base.url_masked)
    now = now or datetime.now()
    rows = probe_rows()
    result = {"probe": base.url_masked, "rows": len(rows)}

    def fail(n, name, detail):
        rep.step(n, name, False, detail)
        _record_failure(root, ad, name, detail)
        rep.lines.append("NOT PROVED: %s. The engine stays unprotected: its commands pass with a warning." % name)
        return 1, rep.text()

    # 1 fill
    try:
        err = ad.probe_fill(base, settings, tools, rows)
        got, why = (None, err) if err else ad.probe_read(base, settings, tools)
    except Exception as e:
        return fail(1, "fill", "%s: %s" % (e.__class__.__name__, db.mask_secrets(str(e))[:200]))
    if err or got is None:
        return fail(1, "fill", why or "the adapter could not fill the probe")
    d = diff_rows(rows, [tuple(r) for r in got])
    if d:
        return fail(1, "fill", "the adapter reads back what it wrote differently: " + "; ".join(d))
    rep.step(1, "fill", True, "%d rows written and read back" % len(rows))
    # 2 copy
    res = db.copy_base(base, root, settings, None, enforce_limits=False, session=session, via="prove",
                       command="prove " + ad.id, now=now)
    if res["status"] != "ok" or not res.get("indexed") or not res.get("path") or not os.path.isfile(res["path"]):
        return fail(2, "copy", res.get("msg") or res["status"])
    result["copy_id"] = res["id"]
    rep.step(2, "copy", True, "%s, %d bytes, verified: %s" % (res["id"], res["copy_size"], res.get("verified", "?")))
    # 3 destroy
    try:
        err = ad.probe_destroy(base, settings, tools)
        after, why = ad.probe_read(base, settings, tools)
    except Exception as e:
        return fail(3, "destroy", "%s: %s" % (e.__class__.__name__, db.mask_secrets(str(e))[:200]))
    if err:
        return fail(3, "destroy", err)
    if after is not None and diff_rows(rows, [tuple(r) for r in after]) == []:
        return fail(3, "destroy", "the rows are still there after the destroy step: a proof of a restore needs something lost first")
    rep.step(3, "destroy", True, "the probe no longer holds the rows (%s)" % ("table gone" if after is None else "%d rows left" % len(after)))
    # 4 restore (the same path `restore` takes)
    status, msg = dbcli.restore_copy(res["id"], settings, to=None, session=session, now=now)
    if status != "ok":
        return fail(4, "restore", msg)
    op = None
    try:
        recs, _bad = store._read_jsonl(db.index_path(root))
        for _n, r in recs:
            if r.get("op") == "restore" and r.get("of") == res["id"]:
                op = r
    except OSError:
        pass
    if op is None:
        return fail(4, "restore", "the restore was made but no record of it was found, so the restored database cannot be located")
    rep.step(4, "restore", True, op.get("to", "?"))
    result["restored_to"] = op.get("to", "")
    # 5 compare
    try:
        nb = ad.restored_base(base, op)
        back, why = ad.probe_read(nb, settings, tools)
    except Exception as e:
        return fail(5, "compare", "%s: %s" % (e.__class__.__name__, db.mask_secrets(str(e))[:200]))
    if back is None:
        return fail(5, "compare", why or "the restored database could not be read")
    d = diff_rows(rows, [tuple(r) for r in back])
    if d:
        return fail(5, "compare", "; ".join(d))
    rep.step(5, "compare", True, "%d rows, every one equal" % len(back))
    full = engines.write_proof(root, ad, dict(result, steps=["fill", "copy", "destroy", "restore", "compare"]))
    rep.lines.append("PROVED %s%s: the proof is in %s (sha256 %s, contract %d)." % (
        ad.id, "" if ad.builtin else " adapter " + os.path.basename(ad.path), engines._proof_file(root), (full["adapter_sha"] or "")[:12], full["contract"]))
    rep.lines.append("What the proof covers: ONE probe table of known rows, round-tripped on this machine by THESE bytes of the adapter file. It is bound to the sha256 of "
                     "the file and the contract version only -- not to the tools of the engine, the interpreter, or a module the adapter imports (a change in those keeps "
                     "the proof), and it is not a statement about every object of a database."
                     if not ad.builtin else "What the proof covers: ONE probe table of known rows, round-tripped on this machine -- not every object of a database.")
    rep.lines.append("Left behind (nothing is deleted by this tool): the restored copy %s; the copy %s in the store; the probe %s. A change of "
                     "the adapter file takes the proof away (it is bound to the sha256)." % (op.get("to", "?"), res["path"], base.url_masked))
    if not ad.builtin:
        rep.lines.append(_ask_activation(ad, cwd, settings, session))
    return 0, rep.text()


def activation_effect(ad):
    return ("the adapter %s (sha256 %s) will count as PROTECTION: a command of its engine will be copied by it, judged by its copies and verified "
            "by its own check, and a destroying command over that engine will no longer be let through with a warning" % (
                os.path.basename(ad.path), (getattr(ad, "sha", "") or "")[:12]))


def _activate_cmd(ad):
    return "python %s activate %s" % (os.path.join(config.KIT_DIR, "shedkeeper.py").replace("\\", "/"), ad.id)


def _ask_activation(ad, cwd, settings, session):
    """The proof passed; it does not protect yet. A code is issued for THESE bytes: the person types it, then `activate` makes the proof count."""
    status, text, _code = confirm.gate("adapter:" + ad.id, ad.sha, settings, cwd, session, effect=activation_effect(ad), cmd=_activate_cmd(ad),
                                       what="would make this adapter count as protection", issue_only=True, verb="activate")
    if status == "ask":
        return ("The proof alone does NOT protect yet: an adapter counts as protection only when the PERSON confirms it. " + text.replace("NOT APPLIED: ", "NOT ACTIVE YET: "))
    return "The proof is written, but no confirmation code could be issued (%s); `activate %s` will say what is needed." % (text[:200], ad.id)


def activate(engine_id, cwd, settings, session=""):
    """-> (exit code, text). The person confirms the proof of an adapter of the user for its CURRENT bytes: 0 activated (or active already);
    4 not applied, a code was issued; 1 refused (no proof, not the person's word, the code could not be spent, the store cannot be used)."""
    ad = engines.get(engine_id)
    if ad is None:
        broke = engines.broken_claims(engine_id)
        if broke:
            return 1, "REFUSED the adapter file %s failed to load: %s" % (broke[0].path, broke[0].error)
        return 1, "REFUSED no engine %r: the built-in ones are postgres and sqlite; an adapter is a .py file in %s (see `status`)" % (engine_id, engines.adapters_dir())
    if ad.builtin:
        return 0, "%s is built in: nothing to activate (the tests of the kit run the same proof before every release)" % ad.id
    dbcli.use_proofs(cwd, settings)
    state, _rec = engines.proof_state(ad)
    if state == "active":
        return 0, "ACTIVATED %s already: the proof for the current bytes of %s was confirmed by the person; nothing to do." % (ad.id, os.path.basename(ad.path))
    if state == "unreadable":
        return 1, ("REFUSED the proofs cannot be read (%s), so it is not known what was proved of %s: run `python %s quarantine` (it moves the damaged lines to "
                   "proofs.jsonl.bad -- nothing is deleted), then `prove %s` again." % (engines.proofs_problem(ad=ad), ad.id, os.path.join(config.KIT_DIR, "shedkeeper.py").replace("\\", "/"), ad.id))
    if state != "proved":
        return 1, ("REFUSED there is no proof of %s for the CURRENT bytes of %s (%s): run `python %s prove %s --target <a probe database>` first." % (
            ad.id, os.path.basename(ad.path), "the last run failed" if state == "failed" else "none was made",
            os.path.join(config.KIT_DIR, "shedkeeper.py").replace("\\", "/"), ad.id))
    status, text, code = confirm.gate("adapter:" + ad.id, ad.sha, settings, cwd, session, effect=activation_effect(ad), cmd=_activate_cmd(ad),
                                      what="would make this adapter count as protection", verb="activate")
    if status == "ask":
        return confirm.EXIT_ASK, text
    if status != "apply":
        return 1, text
    if not confirm.spend(code, cwd, settings):
        return 1, "REFUSED the confirmation code %s could not be marked as used in the store, so nothing was activated. Ask the user for a new code." % code
    try:
        root = store.store_root(cwd, settings)
        engines.write_activation(root, ad, {"code": code})
    except Exception as e:
        return 1, "REFUSED the activation could not be written (%s: %s); the code is used up: ask the user for a new one." % (e.__class__.__name__, e)
    return 0, ("ACTIVATED %s: the person confirmed the proof for the bytes of %s (sha256 %s) with %s. From now on a destroying command over this engine "
               "is copied by the adapter first." % (ad.id, os.path.basename(ad.path), ad.sha[:12], code))


def _record_failure(root, ad, step, detail):
    try:
        store._append_jsonl(engines._proof_file(root), lambda n: {
            "event": "prove_failed", "engine": ad.id, "adapter_sha": getattr(ad, "sha", "builtin"), "contract": engines.CONTRACT_VERSION,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "step": step, "why": db.mask_secrets(str(detail))[:300]})
    except Exception:
        pass
