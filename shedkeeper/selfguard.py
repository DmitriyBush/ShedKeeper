"""selfguard.py -- the agent does not switch the protection off by an ORDINARY way (0.3.0; the second task of ShedKeeper: the agent
does not turn the protection off or weaken it by the usual path).

The ordinary ways, read from what agents really do when "the hooks are in the way" (not made up to get round the guard):

  1. an Edit / Write / MultiEdit of a Claude Code settings file (`.claude/settings.json`, `.claude/settings.local.json`, the user's `settings.json`) that
     takes the hooks of ShedKeeper out, switches the plugin off (`enabledPlugins`) or sets `disableAllHooks`;
  2. `python <kit>/install.py uninstall`;
  3. `claude plugin uninstall | remove | disable shedkeeper[@marketplace]`.

Each is stopped like a weakening of a setting: the person's confirmation code (`CONFIRM SK-nnnnnn`, typed in their own message and seen by the hook of the
prompt) is the way on -- the same mechanism as `set` (`confirm.gate`, key `uninstall`). The person can also do it themselves, outside the agent. A settings
edit that does NOT touch the hooks of ShedKeeper is not stopped.

What this does not do (declared in the README, "Risks"): a settings file written by a shell command (`sed -i`, `jq ... > settings.json`), a program that builds
the path itself, a mod of Claude Code, a variable in front of `install.py` -- the protection is the guest of Claude Code, and the settings of Claude Code are the
user's layer. Nothing here raises: a fault is the strict side where the thing was recognised as a settings file, and silence otherwise.
"""
import json
import os
import re

from . import recognize

KEY = "uninstall"                       # the key of the confirmation code (the same journal as `set`, another key)
PLUGIN = "shedkeeper"
KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FALLBACK = re.compile(r"install\.py['\"]?\s+(?:\S+\s+)*uninstall\b|\bplugin\s+(?:uninstall|remove|disable)\s+['\"]?shedkeeper", re.I)

EFFECT = ("ShedKeeper's protection would be switched OFF: no copy before an edit, no trash instead of a deletion, no copy of a database before it is "
          "destroyed")


def _install():
    import sys
    if KIT not in sys.path:
        sys.path.insert(0, KIT)
    import install
    return install


def is_claude_settings(path):
    """Is this path a settings file of Claude Code: `<dir>/.claude/settings.json`, `<dir>/.claude/settings.local.json`, or the `settings.json` in the folder
    of the user's Claude Code configuration?"""
    ap = os.path.normcase(os.path.abspath(path))
    base = os.path.basename(ap)
    if base not in ("settings.json", "settings.local.json"):
        return False
    parent = os.path.dirname(ap)
    if os.path.basename(parent) == ".claude":
        return True
    try:
        return base == "settings.json" and parent == os.path.normcase(os.path.abspath(_install().user_claude_dir()))
    except Exception:
        return False


def _load(text):
    try:
        data = json.loads((text or "").lstrip("\ufeff"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _kit_hooks(data, install):
    n = 0
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if isinstance(hooks, dict):
        for groups in hooks.values():
            for g in groups if isinstance(groups, list) else []:
                for h in (g.get("hooks") or []) if isinstance(g, dict) and isinstance(g.get("hooks"), list) else []:
                    if isinstance(h, dict) and install.is_ours(h.get("command")):
                        n += 1
    return n


def _plugin_on(data):
    ep = data.get("enabledPlugins") if isinstance(data, dict) else None
    if not isinstance(ep, dict):
        return set()
    return {k for k, v in ep.items() if isinstance(k, str) and k.split("@")[0] == PLUGIN and v is True}


def edit_removes_protection(path, tool, tool_input):
    """'' when the edit of a settings file of Claude Code leaves the protection as it is (or the file is not one); else WHAT it does, in plain words."""
    if not is_claude_settings(path):
        return ""
    from . import settings_cli
    install = _install()
    text, new = settings_cli._edited_text(path, tool, tool_input)
    before = _load(text)
    if new is None:
        if before is not None and (_kit_hooks(before, install) or _plugin_on(before)):
            return "the edit of this file could not be worked out here, and the file holds the hooks of ShedKeeper, so it cannot be told whether it takes them out"
        return ""
    after = _load(new)
    if after is None:
        if before is not None and (_kit_hooks(before, install) or _plugin_on(before)):
            return "the file would no longer be valid JSON, and Claude Code would read none of its hooks"
        return ""
    what = []
    nb, na = _kit_hooks(before, install), _kit_hooks(after, install)
    if na < nb:
        what.append("takes %d of the %d hook entries of ShedKeeper out" % (nb - na, nb))
    gone = _plugin_on(before) - _plugin_on(after)
    if gone:
        what.append("switches the ShedKeeper plugin off (%s)" % ", ".join(sorted(gone)))
    if isinstance(after, dict) and after.get("disableAllHooks") is True and not (isinstance(before, dict) and before.get("disableAllHooks") is True):
        what.append("sets disableAllHooks, which switches every hook of Claude Code off")
    return "; ".join(what)


class _Obs(recognize.Deletion):
    """The observer of the scanner: finds the commands that switch ShedKeeper off."""

    def __init__(self, cwd):
        recognize.Deletion.__init__(self)
        self.cwd = cwd
        self.hits = []

    def visit(self, head, args, pre, ctx):
        try:
            self._visit(head, args, ctx)
        except Exception:
            text = (ctx.get("piece", "") or "") + " " + (head or "")
            if _FALLBACK.search(text):
                self.hits.append("a command that switches ShedKeeper off (read with a fault)")

    def _visit(self, head, args, ctx):
        h = (head or "").lower()
        if recognize.PYTHONS.match(h):
            rest = [a for a in args if not a.startswith("-")]
            if rest and os.path.basename(rest[0]).lower() == "install.py" and "uninstall" in rest[1:]:
                want = os.path.normcase(os.path.abspath(os.path.join(KIT, "install.py")))
                got = os.path.normcase(os.path.abspath(os.path.join(ctx.get("cwd") or self.cwd, rest[0])))
                if got == want:
                    self.hits.append("python install.py uninstall")
        elif h in ("claude", "claude-code"):
            low = [a.lower() for a in args]
            if "plugin" in low:
                i = low.index("plugin")
                sub = [a for a in low[i + 1:] if not a.startswith("-")]
                if sub and sub[0] in ("uninstall", "remove", "disable") and any(a.split("@")[0] == PLUGIN for a in sub[1:]):
                    self.hits.append("claude plugin %s %s" % (sub[0], PLUGIN))


def protection_off_commands(command, cwd, shell="bash"):
    """[label] of the commands in a Bash / PowerShell text that switch ShedKeeper off. Never raises: a text the scanner cannot read is judged by a plain match."""
    if not isinstance(command, str) or not command.strip():
        return []
    tool = "PowerShell" if str(shell).lower() in ("powershell", "pwsh", "ps") else "Bash"
    obs = _Obs(cwd or os.getcwd())
    try:
        recognize._walk(command, tool, 0, obs, cwd or os.getcwd())
    except (RecursionError, recognize._Unparsed, ValueError, IndexError):
        if _FALLBACK.search(command):
            obs.hits.append("a command that switches ShedKeeper off (too long or too deeply nested to read)")
    except Exception:
        if _FALLBACK.search(command):
            obs.hits.append("a command that switches ShedKeeper off (read with a fault)")
    return list(dict.fromkeys(obs.hits))
