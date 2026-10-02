"""advice.py -- the words of a stop or a warning, for TWO readers (0.3.0).

The hook does not know the language of the conversation; the agent does. So every text of ShedKeeper that stops or warns about
something has two addressees: the PERSON gets a short English line (`systemMessage`, a fallback), the AGENT gets the FACTS and the lawful ways on
(`permissionDecisionReason` of a refusal, `additionalContext` of a pass) that this module builds -- no order about how the agent behaves:

    What happened: <what happened and why>. The user decides whether they do it themselves or have the agent do it. Options: (a) the AGENT
    does it: <the exact command>; (b) the USER does it: <what to do>; (c) a SETTING: `python <kit>/shedkeeper.py set <key> <value>` -- <what
    stops being protected>. A setting is changed with `python <kit>/shedkeeper.py set <key> <value>`; one that weakens protection needs the
    user's confirmation code (`CONFIRM SK-nnnnnn`, typed in their own message).

Only the options that really apply to the case are listed (a deleted database has no "the hook copies it itself", an engine that is
not supported has no `dbcopy`). The texts that were there before (what happened, the ready command) are not replaced: the block is
ADDED to them. `has_block` tells whether a text already carries it; `generic` is the block of a refusal that has no special one.
"""
import os
import re

from . import config

MARK = "What happened:"
ASK = "The user decides whether they do it themselves or have the agent do it."


def kit_py():
    return os.path.join(config.KIT_DIR, "shedkeeper.py").replace("\\", "/")


def _q(p, shell="bash"):
    """One word quoted for the shell the command came from (single quotes: nothing inside is expanded)."""
    if shell == "powershell":
        return "'" + p.replace("'", "''") + "'"
    return "'" + p.replace("'", "'\\''") + "'"


def py_cmd(shell="bash", kit=None, quote=None):
    """`python '<kit>/shedkeeper.py'` -- the front of every command of ours that is handed to the agent."""
    q = quote or _q
    return "python %s" % q(kit or kit_py(), shell)


def set_command(key, value, shell="bash", kit=None, quote=None):
    q = quote or _q
    v = str(value)
    return "%s set %s %s" % (py_cmd(shell, kit, quote), key, v if re.fullmatch(r"[\w.:/-]+", v) else q(v, shell))


def effect_text(key, value=None):
    """What stops being protected when `key` is set to `value` (the `@weight` line of the ini, only when this value is the unsafe one),
    or ''."""
    try:
        return config.effect_for(key, str(value))
    except Exception:
        return ""


_GLUE = re.compile(r"(?<![.!?:;])\s+(?=%s)" % re.escape(MARK))


def polish(text):
    """` -- ` between an older text that does not end in punctuation (it ends with a command: the readers of `Run: <command>` cut at ` -- `) and
    the block."""
    return _GLUE.sub(" -- ", text) if isinstance(text, str) else text


def has_block(text):
    return isinstance(text, str) and MARK in text and ASK in text


def block(what, agent=None, user=None, settings=(), no_setting=False, shell="bash", kit=None, quote=None):
    """The facts and the ways on for the agent. `what`: what happened and why, in plain words (a sentence, no final dot needed).
    `agent`: the exact thing the agent can do itself (a command), or None. `user`: what the person can do themselves, or None.
    `settings`: [(key, value), ...] the switches that change the case (each is told with what it stops protecting).
    `no_setting`: say that no setting changes this stop. At least one of the three options is given: a stop with no way forward is
    a text the agent cannot use."""
    what = what.strip().rstrip(".")
    opts, letter = [], iter("abcdefgh")
    if agent:
        opts.append("(%s) the AGENT does it: %s" % (next(letter), agent.strip().rstrip(".")))
    if user:
        opts.append("(%s) the USER does it: %s" % (next(letter), user.strip().rstrip(".")))
    for key, value in settings:
        cmd = set_command(key, value, shell, kit, quote)
        eff = effect_text(key, value)
        opts.append("(%s) a SETTING: `%s`%s" % (next(letter), cmd, (" -- " + eff) if eff else ""))
    out = "%s %s. %s" % (MARK, what, ASK)       # (`what` is in plain words: it is the text the user is told)
    if opts:
        out += " Options: " + "; ".join(opts) + "."
    if settings:
        out += (" A setting is changed with `%s set <key> <value>`; one that weakens protection needs the user's confirmation code (`CONFIRM SK-nnnnnn`, typed in "
                "their own message). `%s settings` lists them all, with what each one switches off." % (py_cmd(shell, kit, quote), py_cmd(shell, kit, quote)))
    elif no_setting:
        out += " No setting changes this stop."
    return out


def generic(why_text, user=None, shell="bash", kit=None, quote=None):
    """The block of a refusal that has no special one: the person decides; no option is invented."""
    what = ("the action was stopped to keep your files and data safe; the reason (in English) is the text before this "
            "line: %s" % re.sub(r"\s+", " ", why_text.replace("shedkeeper:", "")).strip()[:200])
    return block(what, user=user or "do the action themselves, outside the agent, after looking at the reason", shell=shell, kit=kit,
                 quote=quote)
