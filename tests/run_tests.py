"""Tests of the kit. No pytest; prints PASS / FAIL / SKIP per case and a total; the exit code is 0
only when there are 0 FAIL.

Everything happens inside one temporary folder: SHEDKEEPER_TEST_TMP (default: tempfile.mkdtemp()). The
store of a test is a folder inside it, chosen through SHEDKEEPER_STORE_ROOT -- the tests never touch a
real `<X>:\\_shedkeeper`. The tests delete NOTHING (no rmtree, no os.remove): the path of the temporary
folder is printed at the end.

Groups: 1 recognize, 2 store, 3 pre_tool, 4 install, 5 mutations (a guarded place is broken in memory
and the matching test must turn RED: CAUGHT / SLIPPED).
"""
import contextlib
import errno
import io
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
sys.path.insert(0, KIT)

from shedkeeper import config, recognize, store  # noqa: E402

TMP = os.path.abspath(os.environ.get("SHEDKEEPER_TEST_TMP") or tempfile.mkdtemp(prefix="shedkeeper_test_"))
os.makedirs(TMP, exist_ok=True)
RUN = os.path.join(TMP, "run_%s_%d" % (time.strftime("%Y%m%d_%H%M%S"), os.getpid()))
os.makedirs(RUN)
PY = sys.executable
_counter = [0]
# `install` keeps a record of the lines it writes in the personal folder of the store (order 6): a test that installs
# without naming its own store must not make one on the real volume. The tests that care set their own.
os.environ.setdefault("SHEDKEEPER_STORE_ROOT", os.path.join(RUN, "default_store"))


class Skip(Exception):
    pass


class NotMeasured(Exception):
    """The test could not be MEASURED here (a tool it needs is missing): neither green nor red. A run with such a
    result ends with exit code 2 and says so in its total -- a SKIP that ends green is a green over nothing."""


def say(text):
    """print() that cannot die on a character the console code page has no letter for."""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(enc, "backslashreplace").decode(enc))


class T(object):
    """Collects the results of one run of test functions. Statuses: PASS, FAIL (an assertion is red),
    CRASH (the test function itself raised: nothing was decided), SKIP, NOT MEASURED (a tool the test needs is
    missing: the run ends with exit code 2)."""

    def __init__(self, quiet=False):
        self.quiet = quiet
        self.rows = []          # (status, name, detail)

    def _add(self, status, name, detail=""):
        self.rows.append((status, name, detail))
        if not self.quiet:
            say("%-5s %s%s" % (status, name, ("  -- " + detail) if detail and status != "PASS" else ""))

    def check(self, name, cond, detail=""):
        self._add("PASS" if cond else "FAIL", name, "" if cond else str(detail)[:300])
        return bool(cond)

    def skip(self, name, reason):
        self._add("SKIP", name, reason)

    def fails(self):
        return [r for r in self.rows if r[0] == "FAIL"]

    def crashes(self):
        return [r for r in self.rows if r[0] == "CRASH"]

    def run(self, fn):
        try:
            fn(self)
        except Skip as s:
            self._add("SKIP", fn.__name__, str(s))
        except NotMeasured as s:
            self._add("NOT MEASURED", fn.__name__, str(s))
        except Exception:
            self._add("CRASH", fn.__name__, traceback.format_exc().strip().splitlines()[-1])


def sandbox(label):
    _counter[0] += 1
    p = os.path.join(RUN, "%s_%d" % (label, _counter[0]))
    os.makedirs(p)
    return p


def norm(p):
    return os.path.normpath(p).replace("\\", "/")


def bash_path(p):
    p = norm(p)
    return "/" + p[0].lower() + p[2:]


def write(path, text="x\n", binary=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if binary else "w", **({} if binary else {"encoding": "utf-8", "newline": ""})) as fh:
        fh.write(text)
    return path


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@contextlib.contextmanager
def env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def settings(**over):
    s = dict(config.DEFAULTS)
    s.update(over)
    return s


@contextlib.contextmanager
def store_env(sb, **over):
    """A store inside the sandbox + settings without the kit's own shedkeeper.ini. SHEDKEEPER_STORE_ROOT is
    the VOLUME folder; what is yielded is the PERSONAL folder under it (made with the real permissions)."""
    vol = os.path.join(sb, "store")
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no_such.ini")):
        st = settings(**over)
        root = store.store_root("x", st)
        store.ensure_store(root, st)
        yield root, st


# =============================================================================================
# 1. recognize
# =============================================================================================

def t_write_targets(t):
    sb = sandbox("wt")
    c = norm(sb)
    b = bash_path(sb)
    home = os.path.expanduser("~").replace("\\", "/")
    nodir = lambda p: False  # noqa: E731
    cases = [
        ("echo x > out.txt", [("redirect", c + "/out.txt")], [], "bash"),
        ("echo x >> log.txt", [], [], "bash"),
        ("cmd 2>&1 | tail", [], [], "bash"),
        ("cmd 2> err.txt", [("redirect", c + "/err.txt")], [], "bash"),
        ("cmd > /dev/null 2>&1", [], [], "bash"),
        ('python -c "print(1 > 0)"', [], [], "bash"),
        ("grep 'a>b' f > res.md", [("redirect", c + "/res.md")], [], "bash"),
        ('echo x > "%s/a b.md"' % c, [("redirect", c + "/a b.md")], [], "bash"),
        ("echo x >| f.md", [("redirect", c + "/f.md")], [], "bash"),
        ('cd %s && sed -i "s/a/b/" core/x.py dev/y.md' % b,
         [("sed -i", c + "/core/x.py"), ("sed -i", c + "/dev/y.md")], [], "bash"),
        ("sed -n 1,5p core/x.py", [], [], "bash"),
        ("cp a.py %s/core/b.py" % b, [("cp", c + "/core/b.py")], [], "bash"),
        ("cp a.py \\\n  core/b.py", [("cp", c + "/core/b.py")], [], "bash"),
        ("FOO=1 cp a.py core/c.py", [("cp", c + "/core/c.py")], [], "bash"),
        ("echo hi | tee out.md", [("tee", c + "/out.md")], [], "bash"),
        ("echo hi | tee -a out.md", [], [], "bash"),
        ("python - <<'EOF'\nopen('%s/data/s.json','w').write('x')\nEOF" % c,
         [("python", c + "/data/s.json")], [], "bash"),
        ("cat > f.md <<'EOF'\n> not a redirect\nEOF", [("redirect", c + "/f.md")], [], "bash"),
        ("cat <<'EOF' > notes.md\nbody > x\nEOF", [("redirect", c + "/notes.md")], [], "bash"),
        ("cat > a.md <<EOF\n1\nEOF\ncat > b.md <<EOF\n2\nEOF\ncp x.py y.py",
         [("redirect", c + "/a.md"), ("redirect", c + "/b.md"), ("cp", c + "/y.py")], [], "bash"),
        ('cat <<< "hi" > out2.txt', [("redirect", c + "/out2.txt")], [], "bash"),
        ('python -c "x = 1 << SHIFT" && echo 1 > o3.txt', [("redirect", c + "/o3.txt")], [], "bash"),
        ("echo $X > $OUT", [], [], "bash"),
        ("git checkout -- core/x.py", [], [c], "bash"),
        ("git checkout main", [], [], "bash"),
        ("git checkout -b feat", [], [], "bash"),
        ("git checkout .", [], [c], "bash"),
        ("git checkout HEAD core/x.py", [], [c], "bash"),
        ("git restore --staged x.py", [], [], "bash"),
        ("git restore x.py", [], [c], "bash"),
        ("git reset --hard HEAD~1", [], [c], "bash"),
        ("git -C %s/repos/x reset --hard" % c, [], [c + "/repos/x"], "bash"),
        ("cd %s/repos/y && git reset --hard" % b, [], [c + "/repos/y"], "bash"),
        ('git commit -m "undo: git reset --hard later"', [], [], "bash"),
        ("git clean -fd", [], [], "bash"),
        ("cd dev/x && echo 1 > a.txt && cd /c/tmp && echo 2 > b.txt",
         [("redirect", c + "/dev/x/a.txt"), ("redirect", "C:/tmp/b.txt")], [], "bash"),
        ("(cd dev/z && echo 1 > q.txt)", [("redirect", c + "/dev/z/q.txt")], [], "bash"),
        ("cd core && python - <<'EOF'\nopen('s.json','w')\nEOF", [("python", c + "/core/s.json")], [], "bash"),
        ("Set-Content -Path C:/t/a.txt -Value 1", [("ps", "C:/t/a.txt")], [], "powershell"),
        ("Copy-Item C:\\a\\x.txt C:\\b\\y.txt", [("ps", "C:/b/y.txt")], [], "powershell"),
        ("Copy-Item -Force a.txt b.txt", [("ps", c + "/b.txt")], [], "powershell"),
        ("Set-Content -NoNewline s.txt -Value x", [("ps", c + "/s.txt")], [], "powershell"),
        ("Out-File -FilePath C:\\x\\y.md -Encoding utf8", [("ps", "C:/x/y.md")], [], "powershell"),
        ("curl -sL https://e.x/a -o dl.bin", [("download", c + "/dl.bin")], [], "bash"),
        ("sudo /bin/cp a.py core/d.py", [("cp", c + "/core/d.py")], [], "bash"),
        ("for f in a; do sed -i s/a/b/ core/e.py; done", [("sed -i", c + "/core/e.py")], [], "bash"),
        ('bash -c "echo 1 > inner.md"', [("redirect", c + "/inner.md")], [], "bash"),
        ('pwsh -Command "Set-Content -Path p.txt -Value 1"', [("ps", c + "/p.txt")], [], "bash"),
        ("perl -pi -e 's/a/b/' core/f.pl", [("perl -i", c + "/core/f.pl")], [], "bash"),
        ("dd if=/dev/zero of=disk.img bs=1 count=1", [("dd", c + "/disk.img")], [], "bash"),
        ("Get-Date | Tee-Object -FilePath t.log", [("ps", c + "/t.log")], [], "powershell"),
        ("iwr https://e.x -OutFile w.bin", [("ps", c + "/w.bin")], [], "powershell"),
        ("Get-Process | Export-Csv -Path p.csv -NoTypeInformation", [("ps", c + "/p.csv")], [], "powershell"),
        ("x > ~/notes/m.md", [("redirect", home + "/notes/m.md")], [], "bash"),
        ("", [], [], "bash"),
    ]
    for cmd, exp, rb, sh in cases:
        got, grb = recognize.write_targets(cmd, c, shell=sh, isdir=nodir)
        exp_n = [(f, norm(p)) for f, p in exp]
        rb_n = [norm(p) for p in rb]
        t.check("write_targets: " + cmd[:60].replace("\n", " | "), got == exp_n and [norm(x) for x in grb] == rb_n,
                "got %r %r, wanted %r %r" % (got, grb, exp_n, rb_n))
    # cp / mv into an existing folder: folder/name of the source
    got, _ = recognize.write_targets("cp a.py dest", c, isdir=lambda p: norm(p) == c + "/dest")
    t.check("write_targets: cp into an existing folder -> folder/name", got == [("cp", c + "/dest/a.py")], got)
    # a mask is expanded against real files
    write(os.path.join(sb, "m1.txt"))
    write(os.path.join(sb, "m2.txt"))
    got, _ = recognize.write_targets("sed -i s/a/b/ m*.txt", c)
    t.check("write_targets: sed -i mask expands", sorted(p for _f, p in got) == [c + "/m1.txt", c + "/m2.txt"], got)


# Deletion corpus: (command, tool, is a deletion, label). The labels say which property is guarded.
DELETE_CASES = [
    ("rm -rf data/x", "Bash", True, "M1 head"),
    ("cd D:/proj && rm -f a.txt", "Bash", True, "M1 second segment"),
    ("echo x; /usr/bin/rm a", "Bash", True, "M1 path to the eraser"),
    ('python tools/notes.py --reflection "removed rm of temp files"', "Bash", False, "CANNOT word in an argument"),
    ('grep -n "rm -rf" notes.md', "Bash", False, "CANNOT word in a pattern"),
    ("cat > notes.md <<'X'\nrm -rf data\nX", "Bash", False, "CANNOT heredoc body written to a file"),
    ("git rm --cached a.txt", "Bash", False, "CAN git rm --cached"),
    ("git status; rm -rf x", "Bash", True, "M1 git in the line does not hide rm"),
    ("robocopy data/src data/dst /MIR", "Bash", True, "M2 robocopy /MIR"),
    ("robocopy data/src data/dst /E", "Bash", False, "CAN robocopy without erasing switches"),
    ("git clean -fdx", "Bash", True, "M2 git clean"),
    ("git clean -n", "Bash", False, "CAN git clean -n"),
    ("find . -name '*.tmp' -delete", "Bash", True, "M2 find -delete"),
    ("find . -name '*.tmp' -exec rm {} \\;", "Bash", True, "M1 find -exec"),
    ("find . -name '*.py' -exec grep -l x {} \\;", "Bash", False, "CAN find -exec without erasing"),
    ("python -c \"import shutil; shutil.rmtree('data/x')\"", "Bash", True, "M3 python -c"),
    ("python -c \"import os; os.system('rm -rf data/x')\"", "Bash", True, "M3 os.system"),
    ("python -c \"import subprocess; subprocess.run(['rm', '-rf', 'data/x'])\"", "Bash", True, "M3 subprocess list"),
    ('python -c "a=[1,2]; a.remove(1); print(a)"', "Bash", False, "CANNOT list.remove is not deletion"),
    ("python - <<'PY'\nfrom pathlib import Path\nPath('x').unlink()\nPY", "Bash", True,
     "M3 python heredoc body is judged (the kit judges it; the source system does not)"),
    ("bash <<'SH'\nrm -rf data/x\nSH", "Bash", True, "M1 heredoc body read by a shell"),
    ("python -c \"from pathlib import Path; Path('x').unlink()\"", "Bash", True, "M3 pathlib in python -c"),
    ("python - <<'PY'\nprint('rm -rf /')\nPY", "Bash", False, "CANNOT word in a code string"),
    ("python scripts/x.py --clean", "Bash", False, "CANNOT script file is not seen (declared border)"),
    ('bash -c "rm -rf x"', "Bash", True, "M1 shell -c string"),
    ("echo $(rm -rf x)", "Bash", True, "M1 substitution"),
    ("ls | xargs -n 1 rm", "Bash", True, "M1 xargs"),
    ("for f in a b; do rm $f; done", "Bash", True, "M1 do"),
    ("timeout 30 rm -rf x", "Bash", True, "M1 wrapper"),
    ("mv data/x D:/bin/", "Bash", False, "CAN mv is not deletion"),
    ("mv C:/a/*.jsonl D:/bin/b/ && rmdir C:/a", "Bash", True, "rmdir of an empty folder is a removal too"),
    ("rmdir -p a/b/c", "Bash", True, "rmdir -p can take parent folders"),
    ("rd emptydir", "Bash", True, "cmd rd without /s is a removal too"),
    ("cmd //c rd /s /q C:/a", "Bash", True, "M1 rd /s deletes a tree"),
    ("cmd /c del C:/a/x.txt", "Bash", True, "M1 cmd /c del"),
    ("cmd //c echo del x", "Bash", False, "CANNOT cmd echo of a word"),
    ("rmdir data/x", "PowerShell", True, "M4 rmdir in PowerShell = Remove-Item"),
    ("Remove-Item -Recurse -Force data/x", "PowerShell", True, "M4 PowerShell"),
    ("Get-ChildItem *.tmp | Remove-Item", "PowerShell", True, "M4 PowerShell pipeline"),
    ("Get-ChildItem *.tmp | ForEach-Object { Remove-Item $_ }", "PowerShell", True, "M4 script block"),
    ('Write-Output "Remove-Item x"', "PowerShell", False, "CANNOT PowerShell word"),
    ('powershell -Command "Remove-Item x"', "Bash", True, "M1 powershell -Command"),
    ("echo $(rm -rf x", "Bash", True, "border: an unclosed substitution"),
    ("echo " + "a" * (recognize.MAX_PARSE_LEN + 10) + " && rm x", "Bash", True, "border: longer than the limit -> coarse"),
    ("$(" * 6 + "rm -rf x" + ")" * 6, "Bash", True, "border: deeper than the limit -> coarse"),
    ("(cd data && ls); rm -rf x", "Bash", True, "H1 glued );"),
    ("(gci *.tmp)|ri", "PowerShell", True, "H1 glued )| PowerShell"),
    ("gci *.tmp | %{rm $_}", "PowerShell", True, "H2 block without a space"),
    ("powershell -NoProfile Remove-Item -Recurse -Force C:/x", "Bash", True, "I2 powershell without -Command"),
    ("eval rm -rf x", "Bash", True, "I3 eval"),
    ('cleanup() { rm -rf "$t"; }; cleanup', "Bash", True, "I3 function"),
    ("timeout -s KILL 5 rm -rf x", "Bash", True, "I3 wrapper flag with a value"),
    ("$null = Remove-Item x", "PowerShell", True, "I3 PowerShell assignment"),
    ("[void](Remove-Item x)", "PowerShell", True, "I3 PowerShell cast"),
    ('[System.IO.File]::Delete("C:/x.txt")', "PowerShell", True, "PowerShell .NET Delete"),
    ('[IO.Directory]::Delete("C:/x", $true)', "PowerShell", True, "PowerShell .NET Directory.Delete"),
    ('[IO.File]::ReadAllText("C:/x.txt")', "PowerShell", False, "control: .NET read"),
    ('(( n = 1<<3 )); bash -c "rm -rf data/x"', "Bash", True, "H3b the coarse/heredoc scan does not hide it"),
    ('bash -o pipefail -c "rm -rf x"', "Bash", True, "M4 bash -o pipefail -c"),
    ('bash -euc "rm -rf x"', "Bash", True, "M4 bash -euc"),
    ("cmd //c rd /s/q C:/x", "Bash", True, "M4 rd /s/q"),
    ("robocopy a b //MIR", "Bash", True, "M4 robocopy //MIR"),
    ("git --work-tree w clean -f", "Bash", True, "M4 git --work-tree"),
    ('python -c "import os; [os.remove(f) for f in $FILES]"', "Bash", True, "M4 python -c unparsable"),
    ('echo "(rm -rf x)"', "Bash", False, "control: parentheses in quotes"),
    ("ls | xargs -I {} echo {}", "Bash", False, "control: xargs -I {}"),
    ("awk '{print $1}' f.txt", "Bash", False, "control: awk {print}"),
    ("Write-Output (1+1)", "PowerShell", False, "control: PowerShell (1+1)"),
    # mentions are not deletions (the spec's own list)
    ("grep rm file", "Bash", False, "mention: grep rm file"),
    ('echo "rm -rf /"', "Bash", False, "mention: echo \"rm -rf /\""),
    ('git commit -m "remove rm"', "Bash", False, "mention: git commit -m \"remove rm\""),
    ("cat del.txt", "Bash", False, "mention: cat del.txt"),
    ("cat rm.txt", "Bash", False, "mention: a file named rm.txt"),
    ("ls rm", "Bash", False, "mention: rm as an argument"),
    ("python -c \"print('os.remove(x)')\"", "Bash", True, "border: a text search cannot tell code from a string inside python -c"),
    ("rm a.txt 2>/dev/null", "Bash", True, "redirect after rm"),
    ("del /q C:\\x\\y.txt", "Bash", True, "cmd del with a switch"),
    ("unlink a.txt", "Bash", True, "unlink"),
    ("shred -u a.txt", "Bash", True, "shred"),
    ("git rm a.txt", "Bash", True, "git rm without --cached"),
    ("sudo rm -rf x", "Bash", True, "sudo"),
    ("env FOO=1 rm x", "Bash", True, "env"),
    ("nohup rm -rf x &", "Bash", True, "nohup and background"),
    ("bash <<< 'rm -rf x'", "Bash", True, "here-string read by a shell"),
    ("sh -c 'ls; rm x'", "Bash", True, "sh -c with a list"),
    ("pwsh -c \"Remove-Item x\"", "Bash", True, "pwsh -c"),
    ("erase a.txt", "Bash", True, "erase"),
    ("npm start & rm -rf dist", "Bash", True, "a single & separates commands (background)"),
    ('cmd /c "echo x & del a.txt"', "Bash", True, "cmd: a single & separates commands"),
    ("make build 2>&1 | tee build.log", "Bash", False, "CAN 2>&1 is a redirect, not a separator"),
    ("ls &> out.txt", "Bash", False, "CAN &> is a redirect, not a separator"),
    ("& \"C:/tools/app.exe\" --flag", "PowerShell", False, "CAN PowerShell call operator &"),
]


def t_delete_cases(t):
    sb = sandbox("del")
    c = norm(sb)
    for cmd, tool, want, label in DELETE_CASES:
        got = recognize.delete_targets(cmd, c, tool).found
        t.check("delete_targets [%s] %s" % (label, cmd[:50].replace("\n", " | ")), got == want,
                "got %r wanted %r" % (got, want))


def t_delete_paths(t):
    sb = sandbox("delp")
    c = norm(sb)
    write(os.path.join(sb, "a.txt"))
    write(os.path.join(sb, "b.tmp"))
    write(os.path.join(sb, "c.tmp"))
    d = recognize.delete_targets("rm a.txt", c)
    t.check("paths: rm a.txt -> the absolute path, resolved", d.paths == [c + "/a.txt"] and not d.unresolved, d)
    d = recognize.delete_targets('rm -rf "dir with space" other', c)
    t.check("paths: quoted path with a space", d.paths == [c + "/dir with space", c + "/other"], d)
    d = recognize.delete_targets("cd sub && rm x", c)
    t.check("paths: cd changes the base", d.paths == [c + "/sub/x"], d)
    d = recognize.delete_targets("rm *.tmp", c)
    t.check("paths: a mask is expanded", sorted(d.paths) == [c + "/b.tmp", c + "/c.tmp"] and not d.unresolved, d)
    d = recognize.delete_targets("rm *.nomatch", c)
    t.check("paths: a mask without matches -> unresolved", d.unresolved and not d.paths, d)
    d = recognize.delete_targets("rm $X", c)
    t.check("paths: a variable -> unresolved", d.unresolved and not d.paths, d)
    d = recognize.delete_targets("git rm a.txt b.txt", c)
    t.check("paths: git rm lists its paths", d.paths == [c + "/a.txt", c + "/b.txt"], d)
    d = recognize.delete_targets("Remove-Item -Path a.txt,b.txt -Recurse -Force", c, "powershell")
    t.check("paths: Remove-Item -Path a,b", d.paths == [c + "/a.txt", c + "/b.txt"], d)
    d = recognize.delete_targets("Remove-Item -LiteralPath 'a b.txt' -Force", c, "powershell")
    t.check("paths: Remove-Item -LiteralPath", d.paths == [c + "/a b.txt"], d)
    d = recognize.delete_targets("del /q C:\\x\\y.txt", c)
    t.check("paths: cmd del takes the path, not the switch", d.paths == ["C:/x/y.txt"], d)
    d = recognize.delete_targets("cmd //c rd /s /q C:/gone", c)
    t.check("paths: cmd //c rd /s /q", d.paths == ["C:/gone"], d)
    d = recognize.delete_targets("rm -- -weird", c)
    t.check("paths: after -- everything is a path", d.paths == [c + "/-weird"], d)
    d = recognize.delete_targets("ls | xargs rm", c)
    t.check("paths: xargs rm -> unresolved", d.unresolved and not d.paths, d)
    d = recognize.delete_targets("", c)
    t.check("paths: empty command", not d.found, d)
    p, u = recognize.delete_targets("rm a.txt", c)
    t.check("paths: the result unpacks as (paths, unresolved)", p == [c + "/a.txt"] and u is False, (p, u))


# =============================================================================================
# 2. store
# =============================================================================================

from datetime import datetime  # noqa: E402

NOW = datetime(2026, 9, 30, 12, 0, 0)


def lines(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def t_config(t):
    sb = sandbox("cfg")
    s, problems = config.load(os.path.join(sb, "missing.ini"))
    t.check("config: no file -> defaults, no problem", s == config.DEFAULTS and not problems, (s, problems))
    ini = write(os.path.join(sb, "a.ini"),
                "store_dir_name = _mine\nauto_create_store = maybe\nmax_copy_mb = 64\n"
                "on_too_large = allow\ncmd_max_files = lots\nbogus = 1\nnot a setting line\n")
    s, problems = config.load(ini)
    t.check("config: understood values are taken", s["store_dir_name"] == "_mine" and s["max_copy_mb"] == 64
            and s["on_too_large"] == "allow", s)
    t.check("config: auto_create_store not understood -> false (safe), and named",
            s["auto_create_store"] is False and any("auto_create_store" in x for x in problems), problems)
    t.check("config: a number not understood -> the default, and named",
            s["cmd_max_files"] == 200 and any("cmd_max_files" in x for x in problems), problems)
    t.check("config: an unknown key and a broken line are named",
            sum("not understood, ignored" in x for x in problems) == 2, problems)
    ini2 = write(os.path.join(sb, "b.ini"), "on_too_large = sometimes\nstore_dir_name = a/b\n")
    s, problems = config.load(ini2)
    t.check("config: on_too_large not understood -> deny (safe)", s["on_too_large"] == "deny", s)
    t.check("config: a store_dir_name that is not a plain name -> the default",
            s["store_dir_name"] == config.DEFAULT_STORE_DIR and len(problems) == 2, (s, problems))
    ini3 = write(os.path.join(sb, "c.ini"), "auto_create_store = false\n")
    s, _p = config.load(ini3)
    t.check("config: auto_create_store = false is honoured", s["auto_create_store"] is False, s)
    if os.path.exists(os.path.join(KIT, "shedkeeper.ini")):
        shipped, sp = config.load(os.path.join(KIT, "shedkeeper.ini"))
        t.check("config: the shipped shedkeeper.ini parses without problems and holds the defaults",
                not sp and shipped == config.DEFAULTS, (shipped, sp))
    else:
        t.skip("config: the shipped shedkeeper.ini", "shedkeeper.ini not written yet")


def t_store_label(t):
    sb = sandbox("label")
    st = settings()
    with env(SHEDKEEPER_STORE_ROOT=None):
        root = os.path.join(sb, "vol", "_shedkeeper")
        os.makedirs(root)
        write(os.path.join(root, "someone_elses.txt"))
        ok, why = store.ensure_volume(root, st)
        t.check("store: a folder without a label but with content is refused", not ok, why)
        t.check("store: the refusal says how to accept it and how to rename",
                "adopt" in why and "store_dir_name" in why, why)
        t.check("store: nothing was written into the foreign folder", os.listdir(root) == ["someone_elses.txt"],
                os.listdir(root))
        okk, msg = store.adopt(root, st)
        t.check("store: adopt accepts it (a person's decision)", okk and store.label_state(root)[0] == "ours", msg)
        empty = os.path.join(sb, "vol2", "_shedkeeper")
        os.makedirs(empty)
        ok, note = store.ensure_volume(empty, st)
        t.check("store: an empty unmarked folder is taken silently (label put)",
                ok and store.label_state(empty)[0] == "ours" and "taken" in note, (ok, note))
        bad = os.path.join(sb, "vol3", "_shedkeeper")
        os.makedirs(bad)
        write(os.path.join(bad, store.LABEL), "not json")
        ok, why = store.ensure_volume(bad, st)
        t.check("store: an unreadable label is refused", not ok and "unreadable" in why, why)
        filed = os.path.join(sb, "vol4", "_shedkeeper")
        write(filed)
        ok, why = store.ensure_volume(filed, st)
        t.check("store: a file in place of the folder is refused", not ok and "not a folder" in why, why)
        new = os.path.join(sb, "vol5", "_shedkeeper")
        real = store._drive_type
        try:
            store._drive_type = lambda r: 2          # removable
            ok, why = store.ensure_volume(new, st)
            t.check("store: a removable volume -> refused, nothing created",
                    not ok and "removable" in why and not os.path.exists(new), (ok, why))
            store._drive_type = lambda r: 4          # network
            ok, why = store.ensure_volume(new, st)
            t.check("store: a network volume -> refused", not ok and "network" in why, why)
            store._drive_type = lambda r: 3          # fixed
            ok, note = store.ensure_volume(new, settings(auto_create_store=False))
            t.check("store: auto_create_store = false -> refused, nothing created",
                    not ok and not os.path.exists(new), (ok, note))
            ok, note = store.ensure_volume(new, st)
            t.check("store: a fixed volume -> created with a label, said in the note",
                    ok and store.label_state(new)[0] == "ours" and "created" in note, (ok, note))
        finally:
            store._drive_type = real
    ok, why = store.check_available("", st)
    t.check("store: a path without a drive letter has no store", not ok, why)


def t_store_zone(t):
    st = settings()
    with env(SHEDKEEPER_STORE_ROOT=None):
        cases = [("D:/proj/_shedkeeper/trash/a.txt", True), ("D:/_shedkeeper/versions/x/y.txt", True),
                 ("D:/proj/my_shedkeeper_notes/a.txt", False), ("D:/proj/x_shedkeeper.txt", False),
                 ("D:/proj/_shedkeeper", False),               # a FILE named like the store is not the store
                 ("D:/proj/_shedkeeper./a.txt", True),
                 ("D:/proj/_ShedKeeper/a.txt", True),          # Windows: _ShedKeeper IS _shedkeeper
                 ("D:/proj/_SHEDKEEPER/trash/a.txt", True),
                 ("D:/proj/shedkeeper/a.txt", False), ("D:/proj/sub/_shedkeeper/deep/er/a", True)]
        for path, want in cases:
            t.check("store zone: %s -> %s" % (path, want), store.is_store_path(path, st) == want,
                    store.is_store_path(path, st))
    with env(SHEDKEEPER_STORE_ROOT=os.path.join(RUN, "zone_root")):
        t.check("store zone: under SHEDKEEPER_STORE_ROOT", store.is_store_path(os.path.join(RUN, "zone_root", "q", "a"), st))
        t.check("store zone: a sibling of SHEDKEEPER_STORE_ROOT is not the store",
                not store.is_store_path(os.path.join(RUN, "zone_root2", "a"), st))


def t_trash_roundtrip(t):
    sb = sandbox("trash")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "proj", "a.txt"), "hello\n")
        status, msg, info = store.trash(f, "test reason", st, "sess1", NOW)
        t.check("trash: a file is moved", status == "ok" and not os.path.exists(f) and os.path.isfile(info["dst"]), msg)
        t.check("trash: the layout is trash/<day>/<DRIVE>/<path without the letter>",
                norm(info["dst"]) == norm(os.path.join(root, "trash", "2026-09-30", f[0].upper(), f[3:])),
                info["dst"])
        t.check("trash: the content is intact", read(info["dst"]) == "hello\n")
        idx = lines(os.path.join(root, "trash", "_index", "2026-09-30.jsonl"))
        rec = idx[0]
        t.check("trash: the index line has id/src/dst/time/size/reason/session",
                rec["id"] == "t:2026-09-30:1" and norm(rec["src"]) == norm(f) and rec["dst"] == info["dst"]
                and rec["size"] == 6 and rec["reason"] == "test reason" and rec["session"] == "sess1"
                and rec["time"].startswith("2026-09-30T12:00:00"), rec)
        t.check("trash: the labels were created with the store (volume folder and personal folder)",
                store.label_state(os.path.dirname(root))[0] == "ours" and store.user_label_state(root)[0] == "ours")
        hits, probs = store.find("proj/a.TXT", st)
        t.check("trash: find by a part of the path (either slash, any case)",
                len(hits) == 1 and hits[0]["id"] == "t:2026-09-30:1" and hits[0]["present"] and not probs, (hits, probs))
        hits, _p = store.find("test reason", st)
        t.check("trash: find also searches the reason", len(hits) == 1, hits)
        hits, _p = store.find("no-such-thing", st)
        t.check("trash: find with no match -> empty", hits == [], hits)
        status, msg = store.restore("t:2026-09-30:1", st, session="s2", now=NOW)
        t.check("trash: restore renames it back", status == "ok" and read(f) == "hello\n" and not os.path.exists(info["dst"]), msg)
        status, msg = store.restore("t:2026-09-30:1", st, now=NOW)
        t.check("trash: a second restore of the same record is refused", status == "refused" and "already" in msg, msg)
        hits, _p = store.find("proj/a.txt", st)
        t.check("trash: find shows the item as restored", hits and hits[0]["restored"] and not hits[0]["present"], hits)
        d = os.path.join(sb, "proj", "tree")
        write(os.path.join(d, "x", "one.txt"), "1")
        write(os.path.join(d, "two.txt"), "22")
        status, msg, info = store.trash(d, "", st, "", NOW)
        rec = lines(os.path.join(root, "trash", "_index", "2026-09-30.jsonl"))[-1]
        t.check("trash: a folder is moved whole, with its size and file count",
                status == "ok" and rec["kind"] == "dir" and rec["files"] == 2 and rec["size"] == 3
                and os.path.isfile(os.path.join(info["dst"], "x", "one.txt")), (msg, rec))
        for bad, why in ((os.path.join(sb, "nope.txt"), "no such path"), (os.path.join(root, "trash"), "inside the store"),
                         (os.path.splitdrive(sb)[0] + "\\", "drive root"), (sb, "contains the store")):
            status, msg, _i = store.trash(bad, "", st, "", NOW)
            t.check("trash: refused -- %s" % why, status == "refused", msg)


def t_trash_names(t):
    sb = sandbox("names")
    with store_env(sb) as (root, st):
        f = os.path.join(sb, "p", "same.txt")
        dsts = []
        for i in range(3):
            write(f, "v%d" % i)
            status, msg, info = store.trash(f, "", st, "", NOW)
            dsts.append(info.get("dst"))
            t.check("trash: the same path again, time %d -> ok" % i, status == "ok", msg)
        t.check("trash: a taken name gets .1, .2 and nothing is overwritten",
                dsts[1] == dsts[0] + ".1" and dsts[2] == dsts[0] + ".2"
                and [read(x) for x in dsts] == ["v0", "v1", "v2"], dsts)
        real = store.MAX_SUFFIX
        try:
            store.MAX_SUFFIX = 2
            write(f, "v3")
            status, msg, _i = store.trash(f, "", st, "", NOW)
            t.check("trash: no free name after the limit -> refused, the file stays",
                    status == "refused" and "no free name" in msg and os.path.isfile(f), msg)
        finally:
            store.MAX_SUFFIX = real
        # an ancestor that is a FILE in the trash (a file named like the folder needed later)
        g1 = write(os.path.join(sb, "q", "dirlike"), "file")
        store.trash(g1, "", st, "", NOW)
        g2 = write(os.path.join(sb, "q", "dirlike", "inner.txt"), "inner")
        status, msg, info = store.trash(g2, "", st, "", NOW)
        t.check("trash: an ancestor taken by a file gets a suffix, not an error",
                status == "ok" and read(info["dst"]) == "inner", msg)


def t_trash_index_failure(t):
    sb = sandbox("orph")
    with store_env(sb) as (root, st):
        os.makedirs(os.path.join(root, "trash", "_index", "2026-09-30.jsonl"))   # a folder in place of the index
        f = write(os.path.join(sb, "p", "a.txt"), "data")
        status, msg, info = store.trash(f, "r", st, "", NOW)
        t.check("trash: the index cannot be written -> status unindexed, the item is in the trash",
                status == "unindexed" and os.path.isfile(info["dst"]) and not os.path.exists(f), msg)
        t.check("trash: the record is printed and put into _orphans.jsonl",
                "RECORD:" in msg and os.path.isfile(os.path.join(root, "trash", "_index", "_orphans.jsonl")), msg)
        orph = lines(os.path.join(root, "trash", "_index", "_orphans.jsonl"))[0]
        t.check("trash: the orphan record has its own id", orph["id"] == "t:orphans:1", orph)
        status, msg = store.restore("t:orphans:1", st, now=NOW)
        t.check("trash: an orphan comes back by its id", status == "ok" and read(f) == "data", msg)


def t_restore_no_overwrite(t):
    sb = sandbox("norep")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "p", "a.txt"), "old")
        store.trash(f, "", st, "", NOW)
        write(f, "NEW-KEEP-ME")
        status, msg = store.restore("t:2026-09-30:1", st, now=NOW)
        t.check("restore: an occupied place -> refused", status == "refused" and "occupied" in msg, msg)
        t.check("restore: the file that is there is untouched", read(f) == "NEW-KEEP-ME", read(f))
        t.check("restore: the hint names --to", "--to" in msg, msg)
        other = os.path.join(sb, "elsewhere", "a.txt")
        status, msg = store.restore("t:2026-09-30:1", st, to=other, now=NOW)
        t.check("restore: --to puts it elsewhere", status == "ok" and read(other) == "old" and read(f) == "NEW-KEEP-ME", msg)
        status, msg = store.restore("t:2026-09-30:99", st, now=NOW)
        t.check("restore: an unknown id is refused", status == "refused", msg)
        status, msg = store.restore("garbage", st, now=NOW)
        t.check("restore: a malformed id is refused", status == "refused" and "bad id" in msg, msg)


def t_version_copy(t):
    sb = sandbox("ver")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "p", "doc.md"), "one\n")
        r = store.copy_version(f, root, st, "sess", "Edit", NOW)
        t.check("version: a copy is made and verified", r["status"] == "ok" and read(r["copy"]) == "one\n", r)
        t.check("version: the layout is versions/<day>/<DRIVE>/<folders>/<name>.v_<HHMMSS>_<n><ext>",
                norm(r["copy"]) == norm(os.path.join(root, "versions", "2026-09-30", f[0].upper(), f[3:-6],
                                                       "doc.v_120000_1.md")), r["copy"])
        rec = lines(os.path.join(root, "versions", "_index", "2026-09-30.jsonl"))[0]
        import hashlib
        t.check("version: the index line has id/src/copy/time/size/sha256/via/session",
                rec["id"] == "v:2026-09-30:1" and norm(rec["src"]) == norm(f) and rec["size"] == 4
                and rec["sha256"] == hashlib.sha256(b"one\n").hexdigest() and rec["via"] == "Edit"
                and rec["session"] == "sess", rec)
        r2 = store.copy_version(f, root, st, "sess", "Edit", NOW)
        t.check("version: the same content as the last copy -> no new copy, no new index line",
                r2["status"] == "same" and len(lines(os.path.join(root, "versions", "_index", "2026-09-30.jsonl"))) == 1, r2)
        write(f, "two\n")
        r3 = store.copy_version(f, root, st, "sess", "Write", NOW)
        t.check("version: changed content in the same second -> a NEW copy, the old one is not overwritten",
                r3["status"] == "ok" and r3["copy"] != r["copy"] and r3["copy"].endswith("_2.md")
                and read(r["copy"]) == "one\n" and read(r3["copy"]) == "two\n", (r3, r))
        write(f, "one\n")
        r4 = store.copy_version(f, root, st, "sess", "Write", NOW)
        t.check("version: content back to an older state still gets a copy (only the LAST copy is compared)",
                r4["status"] == "ok", r4)
        big = write(os.path.join(sb, "p", "big.bin"), b"\0" * (1536 * 1024), binary=True)
        r5 = store.copy_version(big, root, settings(max_copy_mb=1), "", "Edit", NOW)
        t.check("version: over max_copy_mb -> toolarge, no copy",
                r5["status"] == "toolarge" and r5["copy"] is None, r5)
        r6 = store.copy_version(os.path.join(sb, "p", "gone.md"), root, st, "", "Edit", NOW)
        t.check("version: an unreadable/missing source -> failed", r6["status"] == "failed", r6)


def t_version_verify(t):
    """The copy is compared with the source (size and sha256); a copy that does not match is NOT taken."""
    sb = sandbox("verify")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "p", "doc.md"), "the real content\n")
        real = store.shutil.copy2

        def corrupt_same_size(src, dst, **kw):
            real(src, dst)
            with open(dst, "r+b") as fh:
                fh.write(b"X")             # same size, different bytes

        def truncate(src, dst, **kw):
            real(src, dst)
            with open(dst, "r+b") as fh:
                fh.truncate(3)

        try:
            store.shutil.copy2 = corrupt_same_size
            r = store.copy_version(f, root, st, "", "Edit", NOW)
            t.check("verify: a copy with a flipped byte (same size) is NOT taken", r["status"] == "failed"
                    and "sha256" in r["msg"], r)
            store.shutil.copy2 = truncate
            r = store.copy_version(f, root, st, "", "Edit", NOW)
            t.check("verify: a truncated copy is NOT taken", r["status"] == "failed" and "size" in r["msg"], r)
        finally:
            store.shutil.copy2 = real
        idx = os.path.join(root, "versions", "_index", "2026-09-30.jsonl")
        t.check("verify: a failed copy leaves no index line", not os.path.exists(idx), idx)


def t_restore_version(t):
    sb = sandbox("rver")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "p", "doc.md"), "v1\n")
        r = store.copy_version(f, root, st, "", "Edit", NOW)
        write(f, "v2 - current\n")
        status, msg = store.restore(r["id"], st, session="s", now=NOW)
        t.check("restore version: the old content is back", status == "ok" and read(f) == "v1\n", msg)
        hits, _p = store.find("doc.md", st)
        trashed = [h for h in hits if h["kind"] == "trash"]
        t.check("restore version: the file that was there went to the trash first, with the reason",
                len(trashed) == 1 and read(trashed[0]["where"]) == "v2 - current\n"
                and "replaced by restore" in trashed[0]["reason"], hits)
        t.check("restore version: the copy stays in the store", os.path.isfile(r["copy"]))
        t.check("restore version: no temporary file is left", not [n for n in os.listdir(os.path.dirname(f)) if n.endswith(".part")])
        with open(r["copy"], "r+b") as fh:
            fh.write(b"Z")
        status, msg = store.restore(r["id"], st, now=NOW)
        t.check("restore version: a copy that no longer matches its sha256 is refused", status == "refused"
                and "sha256" in msg and read(f) == "v1\n", msg)
        other = os.path.join(sb, "q", "restored.md")
        f2 = write(os.path.join(sb, "p", "doc2.md"), "x\n")
        r2 = store.copy_version(f2, root, st, "", "Edit", NOW)
        status, msg = store.restore(r2["id"], st, to=other, now=NOW)
        t.check("restore version: --to restores to another place (no displacement)",
                status == "ok" and read(other) == "x\n" and read(f2) == "x\n", msg)
        status, msg = store.restore("v:2026-09-30:999", st, now=NOW)
        t.check("restore version: an unknown id is refused", status == "refused", msg)


def t_find_days(t):
    sb = sandbox("days")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "p", "old.txt"), "o")
        store.trash(f, "", st, "", datetime(2020, 1, 1, 10, 0, 0))
        g = write(os.path.join(sb, "p", "new.txt"), "n")
        store.trash(g, "", st, "", datetime.now())
        h_all, _p = store.find("txt", st)
        h_recent, _p = store.find("txt", st, days=3)
        t.check("find: all days by default", len(h_all) == 2, h_all)
        t.check("find: --days N drops the older days", len(h_recent) == 1 and "new.txt" in h_recent[0]["src"], h_recent)
        os.makedirs(os.path.join(root, "trash", "_index"), exist_ok=True)
        with open(os.path.join(root, "trash", "_index", "2020-01-01.jsonl"), "ab") as fh:
            fh.write(b"{broken\n")
        _h, probs = store.find("txt", st)
        t.check("find: an unreadable index line is reported, not hidden", any("unreadable" in x for x in probs), probs)


def t_unicode_and_long(t):
    sb = sandbox("uni")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "\u043f\u0430\u043f\u043a\u0430", "\u0444\u0430\u0439\u043b \u043d\u043e\u043c\u0435\u0440 \u043e\u0434\u0438\u043d.txt"), "\u0442\u0435\u043a\u0441\u0442\n")
        status, msg, info = store.trash(f, "\u043f\u0440\u0438\u0447\u0438\u043d\u0430", st, "", NOW)
        t.check("cyrillic: a path with Cyrillic goes to the trash", status == "ok" and read(info["dst"]) == "\u0442\u0435\u043a\u0441\u0442\n", msg)
        hits, _p = store.find("\u043d\u043e\u043c\u0435\u0440 \u043e\u0434\u0438\u043d", st)
        t.check("cyrillic: find works on Cyrillic", len(hits) == 1, hits)
        status, msg = store.restore(hits[0]["id"], st, now=NOW)
        t.check("cyrillic: restore works", status == "ok" and read(f) == "\u0442\u0435\u043a\u0441\u0442\n", msg)
        r = store.copy_version(f, root, st, "", "Edit", NOW)
        t.check("cyrillic: a version copy of a Cyrillic path", r["status"] == "ok" and read(r["copy"]) == "\u0442\u0435\u043a\u0441\u0442\n", r)
        # a path longer than 260 characters
        deep = sb
        while len(deep) < 300:
            deep = os.path.join(deep, "segment_%02d_long_folder_name" % (len(deep) % 100))
        lf = os.path.join(deep, "long.txt")
        try:
            os.makedirs(store._lp(deep), exist_ok=True)
            with open(store._lp(lf), "w") as fh:
                fh.write("long\n")
        except OSError as e:
            t.skip("long path (> 260)", "the OS does not allow creating it here: %s" % e)
            return
        status, msg, info = store.trash(lf, "", st, "", NOW)
        t.check("long path: a path over 260 characters goes to the trash (%d chars)" % len(lf),
                status == "ok" and os.path.isfile(store._lp(info["dst"])), msg)
        status, msg = store.restore(info["id"], st, now=NOW)
        t.check("long path: and comes back", status == "ok" and os.path.isfile(store._lp(lf)), msg)
        r = store.copy_version(lf, root, st, "", "Edit", NOW)
        t.check("long path: a version copy of it", r["status"] == "ok", r)


def t_link(t):
    sb = sandbox("link")
    with store_env(sb) as (root, st):
        target = os.path.join(sb, "real_dir")
        write(os.path.join(target, "keep.txt"), "must survive\n")
        link = os.path.join(sb, "the_link")
        try:
            p = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True, text=True, timeout=20)
            made = p.returncode == 0 and os.path.exists(link)
            why = (p.stdout + p.stderr).strip()[:120]
        except Exception as e:
            made, why = False, str(e)
        if not made:
            t.skip("link: a junction goes as a link, its target is not touched", "mklink /J not available: %s" % why)
            return
        status, msg, info = store.trash(link, "", st, "", NOW)
        rec = lines(os.path.join(root, "trash", "_index", "2026-09-30.jsonl"))[-1]
        t.check("link: the junction itself is moved (kind link)", status == "ok" and rec["kind"] == "link"
                and not os.path.lexists(link), (msg, rec))
        t.check("link: the target and its content are untouched", read(os.path.join(target, "keep.txt")) == "must survive\n")
        status, msg = store.restore(info["id"], st, now=NOW)
        t.check("link: the junction comes back and still points to the target",
                status == "ok" and read(os.path.join(link, "keep.txt")) == "must survive\n", msg)


def t_transcript(t):
    sb = sandbox("tr")
    with store_env(sb) as (root, st):
        src = write(os.path.join(sb, "cc", "proj-x", "sess.jsonl"), "1\n2\n")
        os.makedirs(root, exist_ok=True)
        t.check("transcript: first copy", store.save_transcript(src, root, "proj-x", "sess") == "copied")
        dst = os.path.join(root, "transcripts", "proj-x", "sess.jsonl")
        t.check("transcript: the copy equals the source", read(dst) == "1\n2\n")
        t.check("transcript: an equal copy is left alone", store.save_transcript(src, root, "proj-x", "sess") == "same")
        write(src, "1\n2\n3\n")
        t.check("transcript: a longer source replaces it (temp name + os.replace)",
                store.save_transcript(src, root, "proj-x", "sess") == "copied" and read(dst) == "1\n2\n3\n")
        write(src, "1\n")
        t.check("transcript: a shorter source never replaces a longer copy",
                store.save_transcript(src, root, "proj-x", "sess") == "same" and read(dst) == "1\n2\n3\n")
        t.check("transcript: no temporary file is left", not [n for n in os.listdir(os.path.dirname(dst)) if ".part" in n])
        t.check("transcript: a missing source -> failed, no raise",
                store.save_transcript(os.path.join(sb, "nope.jsonl"), root, "p", "s").startswith("failed"))


# =============================================================================================
# 3. pre_tool (and the session hook)
# =============================================================================================

sys.path.insert(0, os.path.join(KIT, "hooks"))
import pre_tool  # noqa: E402

PRE_TOOL = os.path.join(KIT, "hooks", "pre_tool.py")
SESSION = os.path.join(KIT, "hooks", "session.py")
SHEDKEEPER_PY = os.path.join(KIT, "shedkeeper.py")


def clean_env():
    """The environment of a user's machine: our own PYTHONIOENCODING / PYTHONUTF8 / PYTHONPATH are NOT
    there (they hide the console code page and load our own site hooks)."""
    e = dict(os.environ)
    for k in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONPATH"):
        e.pop(k, None)
    return e


def hook_sub(script, data, raw=None, env_extra=None):
    """Run a hook the way Claude Code does: a subprocess in a CLEANED environment, the JSON on stdin as
    raw UTF-8 BYTES (not escaped to ASCII). `raw` (bytes or str) replaces the JSON. `env_extra` is added to
    the cleaned environment. -> (rc, stdout, stderr)."""
    if raw is None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    elif isinstance(raw, str):
        raw = raw.encode("utf-8")
    e = clean_env()
    e.update(env_extra or {})
    p = subprocess.run([PY, script], input=raw, capture_output=True, timeout=60, env=e)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def _bash():
    """A bash to run a ready command in (the shell of Claude Code's Bash tool on Windows), or None."""
    for cand in (shutil.which("bash"), "C:/Program Files/Git/bin/bash.exe"):
        if cand and os.path.isfile(cand):
            return cand
    return None


def _powershell():
    return shutil.which("powershell") or shutil.which("pwsh")


def parse(out):
    return json.loads(out) if out.strip() else None


def verdict(out):
    """'deny' | 'context' | 'allow' from the hook's stdout (a dict, a JSON string, or None)."""
    obj = parse(out) if isinstance(out, str) else out
    if not obj:
        return "allow"
    h = obj.get("hookSpecificOutput", {})
    if h.get("permissionDecision") == "deny":
        return "deny"
    return "context" if h.get("additionalContext") else "allow"


def hook_inproc(data):
    """The same hook, main() called in this process (so a test can break a function in memory)."""
    old_in = sys.stdin
    buf = io.StringIO()
    sys.stdin = io.StringIO(json.dumps(data))
    try:
        with contextlib.redirect_stdout(buf):
            rc = pre_tool.main()
    finally:
        sys.stdin = old_in
    return rc, buf.getvalue()


def edit_event(path, tool="Edit", cwd=None):
    field = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {"tool_name": tool, "tool_input": {field: path}, "cwd": cwd or os.path.dirname(path), "session_id": "s1"}


def bash_event(cmd, cwd, tool="Bash"):
    return {"tool_name": tool, "tool_input": {"command": cmd}, "cwd": cwd, "session_id": "s1"}


def versions_of(root, name):
    out = []
    for dp, _d, fs in os.walk(os.path.join(root, "versions")):
        out += [os.path.join(dp, f) for f in fs if f.startswith(name)]
    return out


@contextlib.contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def t_hook_edit(t):
    sb = sandbox("hedit")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "p", "doc.md"), "before\n")
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
        t.check("hook: Edit of an existing file -> exit 0, not a deny", rc == 0 and verdict(out) != "deny", (rc, out, err))
        vs = versions_of(root, "doc.v_")
        t.check("hook: Edit of an existing file -> a copy exists with the old content",
                len(vs) == 1 and read(vs[0]) == "before\n", vs)
        idx = lines(os.path.join(root, "versions", "_index", store_day() + ".jsonl"))
        t.check("hook: the copy is indexed with via=Edit and the session", idx and idx[-1]["via"] == "Edit"
                and idx[-1]["session"] == "s1", idx)
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f, "Write"))
        t.check("hook: a second Write with the same content -> allowed, no new copy",
                verdict(out) != "deny" and len(versions_of(root, "doc.v_")) == 1, out)
        write(f, "changed\n")
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f, "Write"))
        t.check("hook: a change since the last copy -> a new copy", len(versions_of(root, "doc.v_")) == 2, out)
        nb = write(os.path.join(sb, "p", "book.ipynb"), "{}")
        rc, out, err = hook_sub(PRE_TOOL, edit_event(nb, "NotebookEdit"))
        t.check("hook: NotebookEdit reads notebook_path and copies", verdict(out) != "deny" and len(versions_of(root, "book.v_")) == 1, out)
        rc, out, err = hook_sub(PRE_TOOL, edit_event(os.path.join(sb, "p", "new.md")))
        t.check("hook: Edit of a file that does not exist -> allowed, nothing to copy", verdict(out) == "allow", out)
        rc, out, err = hook_sub(PRE_TOOL, edit_event(os.path.join(sb, "p")))
        t.check("hook: a path that is a folder -> allowed (not a file, nothing to copy)", verdict(out) == "allow", out)
        cyr = write(os.path.join(sb, "\u043f", "\u0434\u043e\u043a.md"), "\u0442\u0435\u043a\u0441\u0442\n")
        rc, out, err = hook_sub(PRE_TOOL, edit_event(cyr))
        t.check("hook: a Cyrillic path is copied", verdict(out) != "deny" and len(versions_of(root, "\u0434\u043e\u043a.v_")) == 1, (out, err))


def store_day():
    return time.strftime("%Y-%m-%d")


def t_hook_deny_shape(t):
    sb = sandbox("hshape")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "a.txt"))
        rc, out, err = hook_sub(PRE_TOOL, bash_event("rm a.txt", sb))
        obj = parse(out)
        h = obj["hookSpecificOutput"]
        t.check("hook: a deny is exit 0 + JSON hookSpecificOutput/hookEventName=PreToolUse/permissionDecision=deny/reason",
                rc == 0 and h["hookEventName"] == "PreToolUse" and h["permissionDecision"] == "deny"
                and isinstance(h["permissionDecisionReason"], str) and h["permissionDecisionReason"], out)
        t.check("hook: the deny output is plain ASCII JSON", out.isascii(), out)
        t.check("hook: nothing was deleted", os.path.isfile(f))


def t_hook_delete(t):
    sb = sandbox("hdel")
    with store_env(sb) as (root, st):
        a = write(os.path.join(sb, "a.txt"), "keep me\n")
        b = write(os.path.join(sb, "b.txt"), "and me\n")
        rc, out, err = hook_sub(PRE_TOOL, bash_event("rm a.txt", sb))
        reason = parse(out)["hookSpecificOutput"]["permissionDecisionReason"]
        t.check("hook: rm a.txt -> deny", verdict(out) == "deny", out)
        t.check("hook: the deny carries a READY command with the absolute shedkeeper.py, trash and the path "
                "(single-quoted for the shell of the Bash tool)",
                "python '" + norm(SHEDKEEPER_PY) + "' trash '" + norm(a) + "' --reason" in reason, reason)
        cmd = reason.split("Run: ", 1)[1].split(" -- ")[0]
        bash = _bash()
        if bash:
            p = subprocess.run([bash, "-c", cmd], capture_output=True, text=True, encoding="utf-8",
                               timeout=60, env=clean_env())
            t.check("hook: the ready command works in bash: the file is in the trash, not deleted",
                    p.returncode == 0 and not os.path.exists(a)
                    and any(read(os.path.join(dp, "a.txt")) == "keep me\n" for dp, _d, fs in os.walk(os.path.join(root, "trash"))
                            if "a.txt" in fs), (p.stdout, p.stderr))
        else:
            t.skip("hook: the ready command works in bash", "no bash on this machine")
            write(a, "keep me\n")
            store.trash(a, "", st, "", NOW)
        rc, out, err = hook_sub(PRE_TOOL, bash_event("rm -rf b.txt a.txt", sb))
        reason = parse(out)["hookSpecificOutput"]["permissionDecisionReason"]
        t.check("hook: several paths -> all in one ready command; a path that no longer exists is not named",
                "'%s'" % norm(b) in reason and norm(a) not in reason, reason)
        rc, out, err = hook_sub(PRE_TOOL, bash_event("rm $SOME_VAR", sb))
        reason = parse(out)["hookSpecificOutput"]["permissionDecisionReason"]
        t.check("hook: paths that cannot be read -> deny with the general text (move to trash with ...)",
                verdict(out) == "deny" and "trash" in reason and "could not be read" in reason, reason)
        rc, out, err = hook_sub(PRE_TOOL, bash_event("ls | xargs rm", sb))
        t.check("hook: xargs rm -> deny", verdict(out) == "deny", out)
        rc, out, err = hook_sub(PRE_TOOL, bash_event("Remove-Item b.txt", sb, "PowerShell"))
        t.check("hook: PowerShell Remove-Item -> deny", verdict(out) == "deny", out)
        rc, out, err = hook_sub(PRE_TOOL, bash_event("rm -f nothing_here.txt", sb))
        t.check("hook: deleting a path that does not exist removes nothing -> allowed", verdict(out) == "allow", out)
        rc, out, err = hook_sub(PRE_TOOL, bash_event('python "%s" trash b.txt --reason x' % norm(SHEDKEEPER_PY), sb))
        t.check("hook: the kit's own command is not a deletion", verdict(out) == "allow", out)
        os.makedirs(root, exist_ok=True)
        inner = write(os.path.join(root, "trash", "x.txt"))
        rc, out, err = hook_sub(PRE_TOOL, bash_event("rm %s" % norm(inner), sb))
        t.check("hook: deleting inside the store -> deny: only a person cleans it",
                verdict(out) == "deny" and "person" in parse(out)["hookSpecificOutput"]["permissionDecisionReason"], out)
        log = lines(os.path.join(root, "log.jsonl"))
        t.check("hook: firings are logged (time, tool, decision, targets, ms)",
                any(r.get("decision") == "deny" and r.get("tool") == "Bash" and "ms" in r and "targets" in r for r in log), log[-2:])


def t_hook_mentions(t):
    """A mention of a deleting word is not a deletion (the guard of the head-based judgement)."""
    sb = sandbox("hment")
    with store_env(sb) as (root, st):
        write(os.path.join(sb, "a.txt"))
        for cmd in ("grep rm a.txt", 'echo "rm -rf /"', 'git commit -m "remove rm"', "cat del.txt", "cat rm.txt"):
            rc, out = hook_inproc(bash_event(cmd, sb))
            t.check("hook (in-process): a mention passes: " + cmd, verdict(out) == "allow" and rc == 0, out)
            d = recognize.delete_targets(cmd, sb)
            t.check("recognize: a mention is not a deletion: " + cmd, not d.found, d)


def t_hook_write(t):
    sb = sandbox("hwrite")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "a.txt"), "original\n")
        rc, out, err = hook_sub(PRE_TOOL, bash_event("echo x > a.txt", sb))
        vs = versions_of(root, "a.v_")
        t.check("hook: `echo x > a.txt` (a.txt exists) -> a copy + allowed",
                verdict(out) != "deny" and len(vs) == 1 and read(vs[0]) == "original\n", (out, vs))
        rc, out, err = hook_sub(PRE_TOOL, bash_event("echo x > fresh.txt", sb))
        t.check("hook: a redirect to a file that does not exist -> allowed, no copy",
                verdict(out) == "allow" and not versions_of(root, "fresh.v_"), out)
        for n in ("f1", "f2", "f3"):
            write(os.path.join(sb, n + ".txt"), n)
        rc, out, err = hook_sub(PRE_TOOL, bash_event("sed -i s/f/g/ f1.txt f2.txt f3.txt", sb))
        t.check("hook: sed -i over three files -> three copies",
                verdict(out) != "deny" and all(len(versions_of(root, n + ".v_")) == 1 for n in ("f1", "f2", "f3")), out)
        write(f, "second state\n")
        rc, out, err = hook_sub(PRE_TOOL, bash_event("Set-Content -Path a.txt -Value y", sb, "PowerShell"))
        vs = versions_of(root, "a.v_")
        t.check("hook: PowerShell Set-Content -> a NEW copy holding the state before the command, allowed",
                verdict(out) != "deny" and len(vs) == 2 and sorted(read(v) for v in vs) == ["original\n", "second state\n"],
                (out, [read(v) for v in vs]))
        rc, out, err = hook_sub(PRE_TOOL, bash_event("cp a.txt dest.txt", sb))
        t.check("hook: cp to a new file -> no copy needed", verdict(out) == "allow", out)
        rc, out, err = hook_sub(PRE_TOOL, bash_event("echo hello", sb))
        t.check("hook: an unrelated command -> silent allow", rc == 0 and out.strip() == "", out)


def t_hook_zone(t):
    """A path is skipped only when a DIRECTORY of it is the store folder -- by name, not by substring."""
    sb = sandbox("hzone")
    with store_env(sb) as (root, st):
        inside = write(os.path.join(sb, "_shedkeeper", "x", "a.txt"), "in store\n")
        rc, out = hook_inproc(edit_event(inside))
        t.check("hook (in-process): a file inside a folder named _shedkeeper is refused (a person changes the store), not versioned",
                verdict(out) == "deny" and "person" in out and not versions_of(root, "a.v_"), out)
        near = write(os.path.join(sb, "my_shedkeeper_notes", "n.txt"), "ordinary\n")
        rc, out = hook_inproc(edit_event(near))
        t.check("hook (in-process): a folder that only CONTAINS the name (my_shedkeeper_notes) is versioned",
                len(versions_of(root, "n.v_")) == 1, out)
        near2 = write(os.path.join(sb, "x_shedkeeper.txt"), "ordinary 2\n")
        rc, out = hook_inproc(edit_event(near2))
        t.check("hook (in-process): a file that only contains the name (x_shedkeeper.txt) is versioned",
                len(versions_of(root, "x_shedkeeper.v_")) == 1, out)


def t_hook_no_copy_denies(t):
    """No copy -> no change (I1). The copy is made to fail in memory; every route must refuse."""
    sb = sandbox("hnocopy")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "a.txt"), "original\n")
        failing = lambda *a, **k: {"status": "failed", "msg": "boom", "copy": None, "id": None, "indexed": False}  # noqa: E731
        with patched(store, "copy_version", failing):
            rc, out = hook_inproc(edit_event(f))
            t.check("hook (in-process): Edit whose copy failed -> deny", verdict(out) == "deny", out)
            t.check("hook (in-process): the reason names the file and the cause",
                    "boom" in out and "a.txt" in out, out)
            rc, out = hook_inproc(bash_event("echo x > a.txt", sb))
            t.check("hook (in-process): a command whose copy failed -> deny", verdict(out) == "deny", out)


def t_hook_broken_store(t):
    sb = sandbox("hbroken")
    f = write(os.path.join(sb, "p", "a.txt"), "original\n")
    filestore = write(os.path.join(sb, "store_is_a_file"), "i am a file\n")
    with env(SHEDKEEPER_STORE_ROOT=filestore, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
        t.check("hook: a broken store (a file instead of the folder) -> deny for an Edit of an existing file",
                rc == 0 and verdict(out) == "deny" and "store" in out, (rc, out, err))
        rc, out, err = hook_sub(PRE_TOOL, bash_event("echo x > a.txt", os.path.dirname(f)))
        t.check("hook: a broken store -> deny for a command that overwrites an existing file", verdict(out) == "deny", out)
        rc, out, err = hook_sub(PRE_TOOL, edit_event(os.path.join(sb, "p", "brand_new.txt")))
        t.check("hook: a broken store does not stop the creation of a new file", verdict(out) == "allow", out)
        rc, out, err = hook_sub(PRE_TOOL, bash_event("echo hi", sb))
        t.check("hook: a broken store does not stop an unrelated command", verdict(out) == "allow", out)


def t_hook_size_limit(t):
    sb = sandbox("hsize")
    big = write(os.path.join(sb, "p", "big.bin"), b"\0" * (1536 * 1024), binary=True)
    for mode, want in (("deny", "deny"), ("allow", "context")):
        ini = write(os.path.join(sb, "%s.ini" % mode), "max_copy_mb = 1\non_too_large = %s\n" % mode)
        with env(SHEDKEEPER_STORE_ROOT=os.path.join(sb, "store_" + mode), SHEDKEEPER_INI=ini):
            rc, out, err = hook_sub(PRE_TOOL, edit_event(big))
            t.check("hook: a file over max_copy_mb with on_too_large = %s -> %s" % (mode, want), verdict(out) == want, out)
            if mode == "deny":
                t.check("hook: the refusal names the setting", "on_too_large" in out and "max_copy_mb" in out, out)
                rc, out, err = hook_sub(PRE_TOOL, bash_event("echo x > big.bin", os.path.dirname(big)))
                t.check("hook: a command over the size limit is refused too", verdict(out) == "deny", out)
            else:
                log = lines(os.path.join(sb, "store_allow", store.login_name(), "log.jsonl"))
                t.check("hook: allow -> the fact of the missing copy is in log.jsonl",
                        any(r.get("event") == "no_copy_allowed" for r in log), log)


def t_hook_ceilings(t):
    sb = sandbox("hceil")
    for n in range(5):
        write(os.path.join(sb, "c%d.txt" % n), "c%d" % n)
    ini = write(os.path.join(sb, "c.ini"), "cmd_max_files = 3\n")
    with env(SHEDKEEPER_STORE_ROOT=os.path.join(sb, "store"), SHEDKEEPER_INI=ini):
        rc, out, err = hook_sub(PRE_TOOL, bash_event("sed -i s/c/d/ c0.txt c1.txt c2.txt c3.txt c4.txt", sb))
        t.check("hook: more files than cmd_max_files -> deny (not a silent skip), the reason names the ceiling",
                verdict(out) == "deny" and "cmd_max_files" in out, out)
        rc, out, err = hook_sub(PRE_TOOL, bash_event("sed -i s/c/d/ c0.txt c1.txt c2.txt", sb))
        t.check("hook: exactly cmd_max_files files -> allowed", verdict(out) != "deny", out)
    ini2 = write(os.path.join(sb, "d.ini"), "cmd_deadline_s = 1\n")
    slow_root = os.path.join(sb, "store2")
    with env(SHEDKEEPER_STORE_ROOT=slow_root, SHEDKEEPER_INI=ini2):
        real = store.copy_version

        def slow(*a, **k):
            time.sleep(0.7)
            return real(*a, **k)

        with patched(store, "copy_version", slow):
            rc, out = hook_inproc(bash_event("sed -i s/c/d/ c0.txt c1.txt c2.txt c3.txt", sb))
        t.check("hook (in-process): copies slower than cmd_deadline_s -> deny naming the ceiling",
                verdict(out) == "deny" and "cmd_deadline_s" in out, out)


def _git(sb, *args):
    return subprocess.run(["git", "-C", sb] + list(args), capture_output=True, text=True, timeout=30)


def t_hook_git_rollback(t):
    sb = sandbox("hgit")
    try:
        ok = _git(sb, "init", "-q").returncode == 0
    except OSError:
        ok = False
    if not ok:
        t.skip("hook: git checkout -- / restore / reset --hard copy the modified files", "git is not available")
        return
    f = write(os.path.join(sb, "tracked.txt"), "committed\n")
    write(os.path.join(sb, "other.txt"), "other\n")
    _git(sb, "add", ".")
    c = _git(sb, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one")
    if c.returncode != 0:
        t.skip("hook: git rollback", "git commit failed here: %s" % c.stderr[:100])
        return
    write(f, "UNCOMMITTED WORK\n")
    with store_env(sb) as (root, st):
        for cmd in ("git checkout -- .", "git restore tracked.txt", "git reset --hard HEAD"):
            rc, out, err = hook_sub(PRE_TOOL, bash_event(cmd, sb))
            t.check("hook: `%s` -> allowed after a copy of the file it would overwrite" % cmd, verdict(out) != "deny", (out, err))
        vs = versions_of(root, "tracked.v_")
        t.check("hook: the copy holds the UNCOMMITTED work (and the unmodified file was not copied)",
                len(vs) == 1 and read(vs[0]) == "UNCOMMITTED WORK\n" and not versions_of(root, "other.v_"), vs)
        rc, out, err = hook_sub(PRE_TOOL, bash_event("git status", sb))
        t.check("hook: git status is silent", out.strip() == "", out)
        nogit = sandbox("hnogit")
        with env(GIT_CEILING_DIRECTORIES=os.path.dirname(nogit)):      # do not look for a repository above it
            rc, out, err = hook_sub(PRE_TOOL, bash_event("git reset --hard", nogit))
        t.check("hook: a rollback outside a repository -> allowed (git itself will fail)", verdict(out) == "allow", out)


def t_hook_failures(t):
    sb = sandbox("hfail")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "a.txt"), "x\n")
        for raw in ("", "not json", "[]", "null", '{"tool_name": ["unhashable"]}', '{"tool_name": "Edit", "tool_input": 5}'):
            rc, out, err = hook_sub(PRE_TOOL, None, raw=raw)
            t.check("hook: odd input %r -> exit 0, no deny, no crash" % raw[:24], rc == 0 and verdict(out) != "deny" and not err.strip(), (rc, out, err[:120]))
        rc, out, err = hook_sub(PRE_TOOL, {"tool_name": "Read", "tool_input": {"file_path": f}})
        t.check("hook: a tool that is not guarded -> silent", rc == 0 and out.strip() == "", out)
        boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # noqa: E731
        with patched(store, "copy_version", boom):
            rc, out = hook_inproc(edit_event(f))
            t.check("hook (in-process): an exception while an Edit is judged -> deny with the error text",
                    verdict(out) == "deny" and "boom" in out, out)
        with patched(recognize, "delete_targets", boom):
            rc, out = hook_inproc(bash_event("echo hello", sb))
            t.check("hook (in-process): an exception in the recognizer, a harmless command -> the command passes",
                    verdict(out) == "allow", out)
            rc, out = hook_inproc(bash_event("rm -rf x", sb))
            t.check("hook (in-process): an exception in the recognizer, `rm -rf x` -> DENY (rough sign of destruction)",
                    verdict(out) == "deny", out)
        with patched(store, "copy_version", boom):
            rc, out = hook_inproc(bash_event("echo x > a.txt", sb))
            t.check("hook (in-process): an exception after a write was recognized -> deny", verdict(out) == "deny", out)
        with patched(store, "log", boom):
            rc, out = hook_inproc(bash_event("rm a.txt", sb))
            t.check("hook (in-process): a failing log does not change the decision", verdict(out) == "deny", out)


def t_session_hook(t):
    sb = sandbox("hsess")
    home = os.path.join(sb, "home")
    os.makedirs(home)
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    cc = os.path.join(sb, "cc", "projects", "proj-folder")
    tp = write(os.path.join(cc, "abc.jsonl"), "1\n2\n")
    write(os.path.join(cc, "crashed.jsonl"), "c1\n")
    with store_env(sb) as (root, st), env(SHEDKEEPER_USER_HOME=home):
        ev = {"hook_event_name": "SessionEnd", "transcript_path": tp, "session_id": "abc", "cwd": proj}
        rc, out, err = hook_sub(SESSION, ev)
        dst = os.path.join(root, "transcripts", "proj-folder", "abc.jsonl")
        t.check("session: SessionEnd copies the transcript to transcripts/<project folder>/<session>.jsonl",
                rc == 0 and out.strip() == "" and read(dst) == "1\n2\n", (rc, out, err))
        t.check("session: SessionEnd copies only the current one", not os.path.exists(os.path.join(root, "transcripts", "proj-folder", "crashed.jsonl")))
        ev["hook_event_name"] = "SessionStart"
        rc, out, err = hook_sub(SESSION, ev)
        t.check("session: SessionStart saves the session that ended without SessionEnd",
                read(os.path.join(root, "transcripts", "proj-folder", "crashed.jsonl")) == "c1\n", err)
        obj = parse(out)
        ctx = obj["hookSpecificOutput"]["additionalContext"] if obj else ""
        t.check("session: SessionStart says ONE line when the hooks are not in the settings",
                obj and obj["hookSpecificOutput"]["hookEventName"] == "SessionStart" and "not found" in ctx and "\n" not in ctx, out)
        rc_i, out_i = subprocess.run([PY, os.path.join(KIT, "install.py"), "install", "--project", proj],
                                      capture_output=True, text=True, encoding="utf-8").returncode, ""
        rc, out, err = hook_sub(SESSION, ev)
        gobj = json.loads(out) if out.strip() else {}
        gctx = gobj.get("hookSpecificOutput", {}).get("additionalContext", "")
        t.check("session: with the hooks installed and a usable store SessionStart gives the short guide, no warning",
                rc_i == 0 and rc == 0 and gctx.startswith("shedkeeper: ") and "WARNING" not in gctx and "not found" not in gctx
                and len(gctx) <= 400 and "systemMessage" not in gobj, (rc_i, len(gctx), out[:300]))
        write(tp, "1\n2\n3\n")
        ev["hook_event_name"] = "SessionEnd"
        hook_sub(SESSION, ev)
        t.check("session: a longer transcript replaces the copy", read(dst) == "1\n2\n3\n")
    filestore = write(os.path.join(sb, "filestore"), "x")
    with env(SHEDKEEPER_STORE_ROOT=filestore, SHEDKEEPER_INI=os.path.join(sb, "no.ini"), SHEDKEEPER_USER_HOME=home):
        ev["hook_event_name"] = "SessionStart"
        rc, out, err = hook_sub(SESSION, ev)
        obj = parse(out)
        t.check("session: SessionStart names an unusable store, never blocks (exit 0)",
                rc == 0 and obj and "store is not usable" in obj["hookSpecificOutput"]["additionalContext"], (rc, out, err))
        rc, out, err = hook_sub(SESSION, None, raw="garbage")
        t.check("session: garbage input -> exit 0, silent", rc == 0 and out.strip() == "", (rc, out))


# =============================================================================================
# 4. install
# =============================================================================================

INSTALL = os.path.join(KIT, "install.py")


NO_BASH_WORDS = "REFUSED: Git Bash was not found"


def inst(*args):
    p = subprocess.run([PY, INSTALL] + list(args), capture_output=True, text=True, encoding="utf-8",
                       timeout=120, env=clean_env())
    if args and args[0] == "install" and NO_BASH_WORDS in p.stdout:
        raise NotMeasured("Git Bash was not found: `install` refuses (order 6, item 6), so the test that installs was NOT measured")
    return p.returncode, p.stdout + p.stderr


def jload(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def ours_entries(data):
    """[(event, matcher, timeout, command)] of the kit's hooks in a settings object."""
    out = []
    for ev, groups in (data.get("hooks") or {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                if "pre_tool.py" in h.get("command", "") or "session.py" in h.get("command", ""):
                    if norm(KIT).lower() in h["command"].replace("\\", "/").lower():
                        out.append((ev, g.get("matcher"), h.get("timeout"), h["command"]))
    return out


def t_install(t):
    sb = sandbox("inst")
    home = os.path.join(sb, "home")
    os.makedirs(home)
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    sp = os.path.join(proj, ".claude", "settings.json")
    with env(SHEDKEEPER_USER_HOME=home):
        rc, out = inst("check", "--project", proj)
        t.check("install: check before installing -> exit 1, says NOT installed", rc == 1 and "NOT installed" in out, out)
        rc, out = inst("install", "--project", proj)
        t.check("install: install into a project without settings -> exit 0, the file is created", rc == 0 and os.path.isfile(sp), out)
        data = jload(sp)
        ents = ours_entries(data)
        t.check("install: three entries (PreToolUse, SessionStart, SessionEnd), timeout 60 on each",
                sorted(e[0] for e in ents) == ["PreToolUse", "SessionEnd", "SessionStart"] and all(e[2] == 60 for e in ents), ents)
        pre = [e for e in ents if e[0] == "PreToolUse"][0]
        t.check("install: the PreToolUse matcher is Edit|Write|NotebookEdit|MultiEdit|Bash|PowerShell",
                pre[1] == "Edit|Write|NotebookEdit|MultiEdit|Bash|PowerShell", pre)
        import install as install_mod
        t.check("install: the command is the Git Bash form with the absolute kit path (hook_command)",
                pre[3] == install_mod.hook_command("pre_tool.py", "PreToolUse", sp)
                and pre[3].startswith("if [ -f '%s' ]; then python '%s'; rc=$?; if [ $rc -eq 126 ]" % ((norm(os.path.join(KIT, "hooks", "pre_tool.py")),) * 2)),
                pre[3][:200])
        before = open(sp, "rb").read()
        backups_before = [n for n in os.listdir(os.path.dirname(sp)) if "shedkeeper-backup" in n]
        rc, out = inst("install", "--project", proj)
        t.check("install: a second install changes nothing (no duplicate, file byte-identical, no new backup)",
                rc == 0 and open(sp, "rb").read() == before and "already installed" in out
                and [n for n in os.listdir(os.path.dirname(sp)) if "shedkeeper-backup" in n] == backups_before, out)
        rc, out = inst("check", "--project", proj)
        t.check("install: check after installing -> exit 0", rc == 0 and "NOT installed" not in out and "python in PATH" in out, out)
        rc, out = inst("uninstall", "--project", proj)
        after = jload(sp)
        t.check("install: uninstall removes the entries of the kit; nothing else was there",
                rc == 0 and not ours_entries(after) and "hooks" not in after, (out, after))
        t.check("install: uninstall made a backup first", [n for n in os.listdir(os.path.dirname(sp)) if "shedkeeper-backup" in n], os.listdir(os.path.dirname(sp)))

    # foreign hooks and other keys stay
    sp2 = os.path.join(sb, "proj2", ".claude", "settings.json")
    other_hook = {"type": "command", "command": "python C:/tools/their_guard.py", "timeout": 5}
    orig = {"permissions": {"allow": ["Bash(ls)"]}, "model": "x",
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [other_hook]}],
                      "Stop": [{"hooks": [{"type": "command", "command": "echo done"}]}]}}
    write(sp2, json.dumps(orig, indent=2))
    with env(SHEDKEEPER_USER_HOME=home):
        rc, out = inst("install", "--project", os.path.dirname(os.path.dirname(sp2)))
        d = jload(sp2)
        t.check("install: someone else's hooks and settings are intact after install",
                rc == 0 and d["permissions"] == orig["permissions"] and d["model"] == "x"
                and d["hooks"]["Stop"] == orig["hooks"]["Stop"]
                and d["hooks"]["PreToolUse"][0] == orig["hooks"]["PreToolUse"][0]
                and len(ours_entries(d)) == 3, d)
        bk = [n for n in os.listdir(os.path.dirname(sp2)) if "shedkeeper-backup" in n]
        t.check("install: a backup of the old settings was made before the edit, and it is the old content",
                len(bk) == 1 and jload(os.path.join(os.path.dirname(sp2), bk[0])) == orig, bk)
        rc, out = inst("install", "--project", os.path.dirname(os.path.dirname(sp2)))
        t.check("install: a second install into settings with foreign hooks does not duplicate", len(ours_entries(jload(sp2))) == 3, out)
        rc, out = inst("uninstall", "--project", os.path.dirname(os.path.dirname(sp2)))
        d = jload(sp2)
        t.check("install: uninstall removes ONLY the entries of the kit (the original object is restored)", rc == 0 and d == orig, d)

    # a group that holds a foreign hook AND ours is split correctly
    sp3 = os.path.join(sb, "proj3", ".claude", "settings.json")
    mixed = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo theirs"},
                                                     {"type": "command", "command": hook_cmd("session.py")}]}]}}
    write(sp3, json.dumps(mixed))
    with env(SHEDKEEPER_USER_HOME=home):
        rc, out = inst("uninstall", "--project", os.path.dirname(os.path.dirname(sp3)))
        d = jload(sp3)
        t.check("install: uninstall keeps the foreign hook that shares a group with ours",
                rc == 0 and d == {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo theirs"}]}]}}, d)

    # a broken settings file is never touched
    sp4 = os.path.join(sb, "proj4", ".claude", "settings.json")
    write(sp4, "{ this is not json")
    with env(SHEDKEEPER_USER_HOME=home):
        for action in ("install", "uninstall"):
            rc, out = inst(action, "--project", os.path.dirname(os.path.dirname(sp4)))
            t.check("install: %s on a settings file that is not JSON -> exit 1, file untouched" % action,
                    rc == 1 and read(sp4) == "{ this is not json", out)
        sp5 = os.path.join(sb, "proj5", ".claude", "settings.json")
        write(sp5, json.dumps({"hooks": {"PreToolUse": "oops"}}))
        rc, out = inst("install", "--project", os.path.dirname(os.path.dirname(sp5)))
        t.check("install: a settings shape that is not understood -> exit 1, file untouched",
                rc == 1 and jload(sp5) == {"hooks": {"PreToolUse": "oops"}}, out)
        # a wrong timeout on our entry is corrected
        sp6 = os.path.join(sb, "proj6", ".claude", "settings.json")
        write(sp6, json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": hook_cmd("pre_tool.py"), "timeout": 5}]}]}}))
        rc, out = inst("install", "--project", os.path.dirname(os.path.dirname(sp6)))
        t.check("install: an entry of the kit with a short timeout is set to 60 (no duplicate)",
                rc == 0 and [e[2] for e in ours_entries(jload(sp6)) if e[0] == "PreToolUse"] == [60], out)
        # --user goes to the user's settings
        rc, out = inst("install", "--user")
        t.check("install: --user writes ~/.claude/settings.json (home from SHEDKEEPER_USER_HOME here)",
                rc == 0 and len(ours_entries(jload(os.path.join(home, ".claude", "settings.json")))) == 3, out)
        rc, out = inst("uninstall", "--user")
        t.check("install: --user uninstall", rc == 0 and not ours_entries(jload(os.path.join(home, ".claude", "settings.json"))), out)
        rc, out = inst("install")
        t.check("install: install without --project/--user is a usage error", rc == 2, out)
        rc, out = inst("install", "--project", os.path.join(sb, "no_such_folder"))
        t.check("install: a project folder that does not exist -> exit 2", rc == 2, out)


def hook_cmd(script):
    return 'python "%s"' % os.path.join(KIT, "hooks", script)


# =============================================================================================
# 6. the fixes after a code review (2026-09-30): each test here is red on the kit as it was before
# =============================================================================================

class _Stdin(object):
    """A stdin with a .buffer of bytes, like the real one."""

    def __init__(self, raw):
        self.buffer = io.BytesIO(raw)


def hook_inproc_bytes(raw):
    """pre_tool.main() in this process with the given RAW BYTES on stdin."""
    old_in = sys.stdin
    buf = io.StringIO()
    sys.stdin = _Stdin(raw)
    try:
        with contextlib.redirect_stdout(buf):
            rc = pre_tool.main()
    finally:
        sys.stdin = old_in
    return rc, buf.getvalue()


@contextlib.contextmanager
def set_attr(obj, name, value):
    """Like patched(), but the name need not exist (a test of a newer API still runs on an older kit)."""
    missing = object()
    old = getattr(obj, name, missing)
    setattr(obj, name, value)
    try:
        yield
    finally:
        if old is missing:
            delattr(obj, name)
        else:
            setattr(obj, name, old)


def copies_of(root, stem):
    return versions_of(root, stem + ".v_")


def existing(paths):
    return sorted(p for p in paths if os.path.isfile(p))


# ---- 1. stdin is read as UTF-8 bytes

def t_hook_utf8_stdin(t):
    sb = sandbox("hutf8")
    with store_env(sb) as (root, st):
        cases = [("cyrillic", "\u043f", "\u0434\u043e\u043a.md"),
                 ("cyrillic-0x98", "\u0418", "\u0418\u0438.md"),
                 ("umlaut", "J\u00f6rg", "\u00fc.md"),
                 ("cjk", "\u65e5\u672c", "\u8a9e.md"),
                 ("emoji", "emoji\U0001F600", "f.md")]
        for label, d, n in cases:
            f = write(os.path.join(sb, d, n), "before\n")
            stem = os.path.splitext(n)[0]
            rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
            t.check("utf-8 stdin [%s]: Edit -> a copy is made, not denied" % label,
                    rc == 0 and verdict(out) != "deny" and len(copies_of(root, stem)) == 1, (rc, out, err[:120]))
            write(f, "changed\n")
            rc, out, err = hook_sub(PRE_TOOL, bash_event('echo x > "%s"' % norm(f), sb))
            t.check("utf-8 stdin [%s]: a Bash redirect to it -> a second copy" % label,
                    verdict(out) != "deny" and len(copies_of(root, stem)) == 2, (rc, out, err[:120]))
        bad = b'{"tool_name": "Edit", "tool_input": {"file_path": "C:/x/\xff.md"}}'
        rc, out, err = hook_sub(PRE_TOOL, None, raw=bad)
        t.check("utf-8 stdin: bytes that are not UTF-8 in an Edit -> DENY (not a silent pass)",
                rc == 0 and verdict(out) == "deny", (rc, out, err[:120]))
        rc, out, err = hook_sub(PRE_TOOL, None, raw=b'{"tool_name": "Bash", "tool_input": {"command": "rm C:/\xff"}}')
        t.check("utf-8 stdin: bytes that are not UTF-8 in a Bash command -> DENY", verdict(out) == "deny", out)
        rc, out, err = hook_sub(PRE_TOOL, None, raw=b'{"tool_name": "Read", "tool_input": {"file_path": "C:/x/\xff.md"}}')
        t.check("utf-8 stdin: bytes that are not UTF-8 in a tool we do not guard -> silent pass",
                rc == 0 and out.strip() == "", out)
        rc, out, err = hook_sub(PRE_TOOL, None, raw=b"\xff\xfe garbage")
        t.check("utf-8 stdin: garbage that names no tool -> silent pass", rc == 0 and out.strip() == "", out)


def t_hook_utf8_inproc(t):
    """The same, in this process (so a mutation of the input reader can be seen by a test)."""
    sb = sandbox("hutf8i")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "\u043f\u0430\u043f\u043a\u0430", "\u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442.md"), "before\n")
        raw = json.dumps(edit_event(f), ensure_ascii=False).encode("utf-8")
        rc, out = hook_inproc_bytes(raw)
        t.check("utf-8 stdin (in-process): a Cyrillic path is copied, not denied",
                verdict(out) != "deny" and len(copies_of(root, "\u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442")) == 1, out)


def t_session_hook_utf8(t):
    sb = sandbox("hsutf8")
    home = os.path.join(sb, "home")
    os.makedirs(home)
    folder = "\u043f\u0430\u043f\u043a\u0430-\u043f\u0440\u043e\u0435\u043a\u0442\u0430"
    sid = "sid-I"      # a session id is a plain name [A-Za-z0-9_-]; the non-ASCII parts here are the folder, the file name and the cwd
    tp = write(os.path.join(sb, "cc", "projects", folder, "\u0441\u0435\u0441\u0441\u0438\u044f.jsonl"), "1\n2\n")
    proj = os.path.join(sb, "\u043f\u0440\u043e\u0435\u043a\u0442")
    os.makedirs(proj)
    with store_env(sb) as (root, st), env(SHEDKEEPER_USER_HOME=home):
        ev = {"hook_event_name": "SessionEnd", "transcript_path": tp, "session_id": sid, "cwd": proj}
        rc, out, err = hook_sub(SESSION, ev)
        dst = os.path.join(root, "transcripts", folder, sid + ".jsonl")
        t.check("session utf-8: a transcript with a non-ASCII path is saved",
                rc == 0 and os.path.isfile(dst) and read(dst) == "1\n2\n", (rc, out, err[:120]))


# ---- 15. the sweep at SessionStart does not need the current transcript to exist

def t_session_sweep_without_current_file(t):
    sb = sandbox("hsweep")
    home = os.path.join(sb, "home")
    os.makedirs(home)
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    cc = os.path.join(sb, "cc", "projects", "proj-folder")
    write(os.path.join(cc, "crashed.jsonl"), "c1\n")
    tp = os.path.join(cc, "brand-new-session.jsonl")            # not created yet
    with store_env(sb) as (root, st), env(SHEDKEEPER_USER_HOME=home):
        ev = {"hook_event_name": "SessionStart", "transcript_path": tp, "session_id": "brand-new-session", "cwd": proj}
        rc, out, err = hook_sub(SESSION, ev)
        t.check("session: SessionStart saves the crashed session although the current transcript does not exist yet",
                rc == 0 and os.path.isfile(os.path.join(root, "transcripts", "proj-folder", "crashed.jsonl")), (rc, out, err[:120]))


# ---- 2. a fault of the recognizer is judged by what the command carries

def t_hook_recognizer_fault(t):
    sb = sandbox("hfault")
    with store_env(sb) as (root, st):
        write(os.path.join(sb, "a.txt"), "x\n")
        boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # noqa: E731
        rough = ["rm -rf x", "echo x > a.txt", "sed -i s/a/b/ a.txt", "cp a.txt b.txt", "mv a.txt b.txt",
                 "git checkout -- .", "git reset --hard", "git clean -fd", "Remove-Item a.txt",
                 "Set-Content -Path a.txt -Value 1", "Out-File a.txt", "Copy-Item a.txt b.txt", "Move-Item a.txt b.txt",
                 "del a.txt", "find . -delete", "tee a.txt"]
        harmless = ["echo hello", "ls -la", "git status", "git log --oneline", "python -V", "pwd"]
        for name in ("delete_targets", "write_targets"):
            with patched(recognize, name, boom):
                for cmd in rough:
                    rc, out = hook_inproc(bash_event(cmd, sb))
                    t.check("recognizer fault (%s): `%s` -> deny" % (name, cmd), verdict(out) == "deny", out[:160])
                for cmd in harmless:
                    rc, out = hook_inproc(bash_event(cmd, sb))
                    t.check("recognizer fault (%s): `%s` -> passes" % (name, cmd), verdict(out) == "allow", out[:160])
        rd = getattr(pre_tool, "rough_destructive", None)
        t.check("rough_destructive: a plain listing is not rough", rd is not None and not rd("ls -la"), "no rough_destructive")
        t.check("rough_destructive: a non-string is not rough", rd is not None and not rd(None), "no rough_destructive")


# ---- 3. a mask cut at the ceiling is a refusal

def t_hook_mask_ceiling(t):
    sb = sandbox("hmask")
    for n in range(250):
        write(os.path.join(sb, "k%03d.txt" % n), "k")
    with store_env(sb) as (root, st):
        rc, out = hook_inproc(bash_event("sed -i s/a/b/ k*.txt", sb))
        t.check("mask: 250 files against the ceiling of 200 -> DENY (was: 200 copies, 50 files unprotected)",
                verdict(out) == "deny", out[:200])
        t.check("mask: nothing is copied before the refusal", not versions_of(root, "k"), len(versions_of(root, "k")))
        rc, out = hook_inproc(bash_event("tee k*.txt", sb))
        t.check("mask: `tee k*.txt` over 250 files -> deny too", verdict(out) == "deny", out[:120])
    d = recognize.delete_targets("rm k*.txt", sb)
    t.check("mask: a deletion mask that names more than the ceiling is UNRESOLVED (not 200 of 250 named)",
            d.unresolved, d)
    notes = []
    try:
        got, _ = recognize.write_targets("sed -i s/a/b/ k*.txt", sb, max_files=10, notes=notes)
    except TypeError as e:
        got = []
        notes = ["write_targets has no max_files/notes: %s" % e]
    t.check("mask: write_targets with max_files=10 returns 11 paths and a note",
            len(got) == 11 and len(notes) == 1 and "more than" in notes[0], (len(got), notes))


# ---- 4. the installed timeout has a budget

def t_install_timeout_budget(t):
    import install as install_mod
    git_total = getattr(pre_tool, "GIT_TOTAL_S", 10)
    need = config.DEFAULTS["cmd_deadline_s"] + git_total + 15
    t.check("timeout: the installed timeout (%d) >= cmd_deadline_s + git budget + 15 s margin (%d)"
            % (install_mod.TIMEOUT_S, need), install_mod.TIMEOUT_S >= need)
    readme = read(os.path.join(KIT, "README.md"))
    ini = read(os.path.join(KIT, "shedkeeper.ini"))
    t.check("timeout: the README names the installed timeout", "%d-second" % install_mod.TIMEOUT_S in readme)
    t.check("timeout: shedkeeper.ini explains the budget of cmd_deadline_s against the timeout",
            "%d s" % install_mod.TIMEOUT_S in ini)


# ---- 5. the store is changed by a person only

def t_hook_store_write_denied(t):
    sb = sandbox("hstorew")
    with store_env(sb) as (root, st):
        inside = write(os.path.join(root, "trash", "_index", "2026-01-01.jsonl"), "{}\n")
        named = write(os.path.join(sb, "_shedkeeper", "x", "a.txt"), "in a folder named like the store\n")
        for tool in ("Edit", "Write", "MultiEdit"):
            rc, out = hook_inproc(edit_event(inside, tool))
            t.check("store: %s of an existing file inside the store -> deny (a person changes the store)" % tool,
                    verdict(out) == "deny" and "person" in out, out[:200])
        rc, out = hook_inproc(edit_event(os.path.join(root, "trash", "brand_new.txt"), "Write"))
        t.check("store: Write of a NEW file inside the store -> deny", verdict(out) == "deny", out[:200])
        nb = write(os.path.join(root, "nb.ipynb"), "{}")
        rc, out = hook_inproc(edit_event(nb, "NotebookEdit"))
        t.check("store: NotebookEdit inside the store -> deny", verdict(out) == "deny", out[:200])
        rc, out = hook_inproc(edit_event(named))
        t.check("store: Edit inside a folder named like the store -> deny", verdict(out) == "deny", out[:200])
        for cmd in ('echo x > "%s"' % norm(inside), 'echo x > "%s/new.txt"' % norm(root),
                    "sed -i s/a/b/ %s" % norm(inside), "cp a.txt %s/trash/copied.txt" % norm(root),
                    "tee %s" % norm(inside)):
            rc, out = hook_inproc(bash_event(cmd, sb))
            t.check("store: `%s` -> deny" % cmd[:60], verdict(out) == "deny" and "person" in out, out[:200])
        rc, out = hook_inproc(bash_event('echo x > "%s/notes/ok.txt"' % norm(sb), sb))
        t.check("store: a redirect to an ordinary path is not touched", verdict(out) != "deny", out[:200])
        ok = write(os.path.join(sb, "notes", "ok2.txt"), "ordinary\n")
        rc, out = hook_inproc(edit_event(ok))
        t.check("store: an ordinary file is still versioned", verdict(out) != "deny" and len(copies_of(root, "ok2")) == 1, out[:200])


# ---- 6. Remove-Item values, pipelines, backtick quotes

def t_delete_pipeline_and_values(t):
    sb = sandbox("delpipe")
    c = norm(sb)
    d = recognize.delete_targets("Get-ChildItem C:\\proj\\old -Recurse | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue",
                                 c, "powershell")
    t.check("delete: a pipeline-fed Remove-Item is UNRESOLVED (its paths come through the pipe)", d.unresolved, d)
    for extra in ("-ErrorAction SilentlyContinue", "-ea Stop", "-WarningAction Ignore", "-ErrorVariable ev",
                  "-OutVariable ov", "-Include *.tmp", "-Exclude *.keep", "-Filter *.log"):
        d = recognize.delete_targets("Remove-Item %s -Path a.txt" % extra, c, "powershell")
        t.check("delete: Remove-Item %s -> the value is not a path" % extra,
                d.paths == [c + "/a.txt"] and not d.unresolved, d)
    d = recognize.delete_targets("Remove-Item -Force nothing.txt", c, "powershell")
    t.check("delete: control -- a plain Remove-Item of one literal path is fully resolved",
            d.paths == [c + "/nothing.txt"] and not d.unresolved, d)
    d = recognize.delete_targets("ls | xargs rm -f nothing.txt", c)
    t.check("delete: `xargs rm -f X` also acts on piped input -> UNRESOLVED", d.unresolved, d)
    d = recognize.delete_targets("cat list.txt | rm known.txt", c)
    t.check("delete: a deleting head fed by a pipe -> UNRESOLVED", d.unresolved, d)
    d = recognize.delete_targets('Write-Host "a`"b"; Remove-Item important.txt', c, "powershell")
    t.check("delete: a backtick-escaped quote in PowerShell does not hide the next command",
            d.paths == [c + "/important.txt"], d)
    d = recognize.delete_targets("Write-Host 'x' `\n; Remove-Item y.txt", c, "powershell")
    t.check("delete: a backtick before a newline (PowerShell line continuation) still reads the command", d.found, d)
    sb2 = sandbox("delpipe2")
    with store_env(sb2) as (root, st):
        rc, out = hook_inproc(bash_event("Get-ChildItem x | Remove-Item -Force -ErrorAction SilentlyContinue", sb2, "PowerShell"))
        t.check("hook: a piped Remove-Item with a common parameter -> deny (was: passed as 'deletes nothing')",
                verdict(out) == "deny", out[:200])
        rc, out = hook_inproc(bash_event("Remove-Item -Force nothing.txt", sb2, "PowerShell"))
        t.check("hook: a fully resolved deletion of a path that does not exist still passes", verdict(out) == "allow", out[:200])


# ---- 7. cmd /c "<one string>" and bash -lc

def t_nested_shells(t):
    sb = sandbox("nested")
    c = norm(sb)
    cases = [('cmd /c "rd /s /q C:\\x"', "Bash", ["C:/x"]),
             ('cmd //c "del /f /q C:\\proj\\a.txt"', "Bash", ["C:/proj/a.txt"]),
             ('cmd /c "cd /d C:\\x && del y"', "Bash", ["C:/x/y"]),
             ('cmd /c "rd /s /q C:\\x"', "PowerShell", ["C:/x"]),
             ('bash -lc "rm -rf C:/z"', "Bash", ["C:/z"]),
             ("cmd /c rd /s /q C:/still-works", "Bash", ["C:/still-works"])]
    for cmd, tool, want in cases:
        d = recognize.delete_targets(cmd, c, tool)
        t.check("nested delete: %s (%s)" % (cmd, tool), d.paths == want, d)
    got, _ = recognize.write_targets('bash -lc "sed -i s/a/b/ f.txt"', c)
    t.check("nested write: bash -lc reads the string", got == [("sed -i", c + "/f.txt")], got)
    got, _ = recognize.write_targets('cmd /c "echo x > f.txt"', c)
    t.check("nested write: cmd /c \"...\" reads the string", got == [("redirect", c + "/f.txt")], got)
    got, _ = recognize.write_targets('sh -ec "echo x > g.txt"', c)
    t.check("nested write: sh -ec reads the string", got == [("redirect", c + "/g.txt")], got)


# ---- 8. recursive copies over an existing folder

def t_copy_recursive(t):
    sb = sandbox("crec")
    c = norm(sb)
    write(os.path.join(sb, "new_src", "a.txt"), "new a")
    write(os.path.join(sb, "new_src", "c.txt"), "new c")
    write(os.path.join(sb, "new_src", "sub", "b.txt"), "new b")
    write(os.path.join(sb, "src", "a.txt"), "old a")
    write(os.path.join(sb, "src", "sub", "b.txt"), "old b")

    def names(cmd, shell="bash"):
        got, _ = recognize.write_targets(cmd, c, shell=shell)
        return existing(p for _f, p in got)

    def notes_of(cmd):
        try:
            nl = []
            recognize.write_targets(cmd, c, notes=nl)
            return nl
        except TypeError as e:
            return ["no notes parameter: %s" % e]

    want = [c + "/src/a.txt", c + "/src/sub/b.txt"]
    t.check("cp -r new_src/. src/ -> the files it overwrites", names("cp -r new_src/. src/") == want, names("cp -r new_src/. src/"))
    t.check("cp -r new_src/* src/ -> the files it overwrites", names("cp -r new_src/* src/") == want, names("cp -r new_src/* src/"))
    t.check("cp -a new_src/. src/ -> the same (-a is recursive)", names("cp -a new_src/. src/") == want)
    ps = "Copy-Item %s\\new_src\\* %s\\src\\ -Recurse -Force" % (sb, sb)
    t.check("Copy-Item new_src\\* src\\ -Recurse -> the same", names(ps, "powershell") == want, names(ps, "powershell"))
    ps2 = "Copy-Item %s\\new_src\\a.txt %s\\src\\" % (sb, sb)
    t.check("Copy-Item a.txt into an existing folder -> folder\\a.txt", names(ps2, "powershell") == [c + "/src/a.txt"], names(ps2, "powershell"))
    t.check("cp new_src src (no -r): a folder is not copied, nothing to list", names("cp new_src src") == [])
    t.check("cp -r new_src fresh/ (the destination does not exist): nothing is overwritten", names("cp -r new_src fresh/") == [])
    write(os.path.join(sb, "src", "new_src", "a.txt"), "already there")
    t.check("cp -r new_src src -> src/new_src/a.txt (a folder into a folder)", names("cp -r new_src src") == [c + "/src/new_src/a.txt"],
            names("cp -r new_src src"))
    t.check("mv new_src src -> the same rule for a folder", names("mv new_src src") == [c + "/src/new_src/a.txt"])
    notes = notes_of("cp -r $SRC src/")
    t.check("cp -r from a variable -> a note (the source cannot be read)", len(notes) == 1 and "cannot be read" in notes[0], notes)
    notes = notes_of("cp $SRC src/")
    t.check("cp (no -r) from a variable -> no note (declared border, not a refusal)", not notes, notes)
    sb2 = sandbox("crec2")
    write(os.path.join(sb2, "new_src", "a.txt"), "new a")
    write(os.path.join(sb2, "new_src", "sub", "b.txt"), "new b")
    write(os.path.join(sb2, "src", "a.txt"), "old a")
    write(os.path.join(sb2, "src", "sub", "b.txt"), "old b")
    with store_env(sb2) as (root, st):
        rc, out, err = hook_sub(PRE_TOOL, bash_event("cp -r new_src/. src/", sb2))
        t.check("hook: cp -r over an existing folder -> a copy of EVERY file it overwrites",
                verdict(out) != "deny" and len(copies_of(root, "a")) == 1 and len(copies_of(root, "b")) == 1
                and read(copies_of(root, "b")[0]) == "old b", (out, err[:120]))
        rc, out, err = hook_sub(PRE_TOOL, bash_event("cp -r $SRC src/", sb2))
        t.check("hook: cp -r from a variable -> deny with the reason", verdict(out) == "deny" and "cannot be listed" in out, out[:200])


# ---- 9. comments

def t_comments(t):
    sb = sandbox("comm")
    c = norm(sb)
    got, _ = recognize.write_targets("# rewrite the user's config\necho new > config.txt", c)
    t.check("comment: an apostrophe in a comment does not hide a later redirect", got == [("redirect", c + "/config.txt")], got)
    got, _ = recognize.write_targets("# don't panic\nsed -i s/a/b/ x.txt\ncp a.txt b.txt", c)
    t.check("comment: ... nor a later sed -i and cp",
            got == [("sed -i", c + "/x.txt"), ("cp", c + "/b.txt")], got)
    got, _ = recognize.write_targets("echo hi # > ignored.txt", c)
    t.check("comment: a redirect inside a comment is not a target", got == [], got)
    got, _ = recognize.write_targets("echo $# > o.txt", c)
    t.check("comment: `$#` is not a comment", got == [("redirect", c + "/o.txt")], got)
    got, _ = recognize.write_targets("echo a#b > o2.txt", c)
    t.check("comment: `a#b` is not a comment", got == [("redirect", c + "/o2.txt")], got)
    got, _ = recognize.write_targets('git commit -m "x # y" && echo 1 > o3.txt', c)
    t.check("comment: a # inside quotes is not a comment", got == [("redirect", c + "/o3.txt")], got)
    d = recognize.delete_targets("ls # done; rm a.txt", c)
    t.check("comment: a deleting word after # is a comment, not a deletion", not d.found, d)
    d = recognize.delete_targets("echo a#b; rm x", c)
    t.check("comment: `a#b; rm x` -- the # is inside a word, rm is real", d.found, d)
    d = recognize.delete_targets("# it's a note\nrm x.txt", c)
    t.check("comment: a deletion on the line after a comment with an apostrophe is seen", d.paths == [c + "/x.txt"], d)
    d = recognize.delete_targets("Get-Date # note\nRemove-Item z.txt", c, "powershell")
    t.check("comment: PowerShell # comment, deletion on the next line", d.paths == [c + "/z.txt"], d)


# ---- 10. git without HEAD, a diff that fails

def t_git_no_head(t):
    sb = sandbox("gnohead")
    try:
        ok = _git(sb, "init", "-q").returncode == 0
    except OSError:
        ok = False
    if not ok:
        t.skip("git without HEAD", "git is not available")
        return
    f = write(os.path.join(sb, "t.txt"), "staged\n")
    _git(sb, "add", "t.txt")
    write(f, "UNSTAGED WORK\n")
    with store_env(sb) as (root, st):
        rc, out, err = hook_sub(PRE_TOOL, bash_event("git checkout -- .", sb))
        vs = copies_of(root, "t")
        t.check("git: a repository WITHOUT a commit -- the file a rollback would overwrite is copied",
                verdict(out) != "deny" and len(vs) == 1 and read(vs[0]) == "UNSTAGED WORK\n", (out, err[:120], vs))
    files, err = pre_tool._git_modified(sb)
    t.check("git: _git_modified lists the unstaged file without HEAD, no error",
            err is None and [norm(x) for x in files] == [norm(f)], (files, err))


def t_git_diff_fails(t):
    class CP(object):
        def __init__(self, rc, out=b""):
            self.returncode, self.stdout, self.stderr = rc, out, b""

    def fake(cmd, **kw):
        if "--show-toplevel" in cmd:
            return CP(0, b"/some/repo\n")
        return CP(128)

    with patched(pre_tool.subprocess, "run", fake):
        files, err = pre_tool._git_modified("/x")
    t.check("git: rev-parse passed but the diff failed -> an ERROR text, not an empty list",
            files == [] and err and "diff" in err, (files, err))

    def slow(cmd, **kw):
        raise pre_tool.subprocess.TimeoutExpired(cmd, 1)

    with patched(pre_tool.subprocess, "run", slow):
        files, err = pre_tool._git_modified("/x")
    t.check("git: a hang -> an error text", files == [] and err, (files, err))

    def missing(cmd, **kw):
        raise FileNotFoundError("git")

    with patched(pre_tool.subprocess, "run", missing):
        files, err = pre_tool._git_modified("/x")
    t.check("git: git not installed -> an error text (the hook refuses)", files == [] and err, (files, err))


# ---- 11. index ids under parallel writers; a lock that cannot be had

PARALLEL_SCRIPT = '''import os, sys, time
sys.path.insert(0, sys.argv[1])
from shedkeeper import store
path, count, start = sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
while time.time() < start:
    pass
for i in range(count):
    store._append_jsonl(path, lambda n: {"id": n, "pid": os.getpid()})
'''


def t_index_parallel(t):
    sb = sandbox("par")
    script = write(os.path.join(sb, "writer.py"), PARALLEL_SCRIPT)
    idx = os.path.join(sb, "idx", "day.jsonl")
    workers, each = 8, 25
    start = time.time() + 4.0
    procs = [subprocess.Popen([PY, script, KIT, idx, str(each), str(start)], env=clean_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(workers)]
    errs = []
    for p in procs:
        _o, e = p.communicate(timeout=120)
        if p.returncode:
            errs.append(e.decode("utf-8", "replace")[-200:])
    recs, bad = store._read_jsonl(idx) if os.path.isfile(idx) else ([], 0)
    ids = sorted(r["id"] for _n, r in recs)
    t.check("index: %d processes x %d appends -> every line readable, every id unique, the numbers run 1..%d"
            % (workers, each, workers * each), not errs and not bad and ids == list(range(1, workers * each + 1)),
            (errs[:1], "unreadable lines: %d" % bad, len(ids), len(set(ids))))


def _hold_index(path):
    """Take the index lock the way another process would (a separate handle). -> the open file."""
    import msvcrt
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+b")
    os.lseek(fh.fileno(), getattr(store, "LOCK_POS", 1 << 40), os.SEEK_SET)
    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    return fh


def _release_index(fh):
    import msvcrt
    os.lseek(fh.fileno(), getattr(store, "LOCK_POS", 1 << 40), os.SEEK_SET)
    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    fh.close()


def t_index_lock_busy(t):
    if os.name != "nt":
        raise Skip("the lock test uses msvcrt (Windows)")
    sb = sandbox("lockbusy")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "p", "a.txt"), "data")
        idx = os.path.join(root, "trash", "_index", "2026-09-30.jsonl")
        fh = _hold_index(idx)
        try:
            with set_attr(store, "LOCK_WAIT_S", 0.3):
                status, msg, info = store.trash(f, "r", st, "", NOW)
        finally:
            _release_index(fh)
        t.check("lock: the day index is held by someone else -> the move is done, status 'unindexed'",
                status == "unindexed" and os.path.isfile(info.get("dst", "")) and not os.path.exists(f), (status, msg))
        orph = os.path.join(root, "trash", "_index", "_orphans.jsonl")
        recs = lines(orph) if os.path.isfile(orph) else []
        t.check("lock: ... and the record is in _orphans.jsonl with its own id", recs and recs[0]["id"] == "t:orphans:1", recs)
        g = write(os.path.join(sb, "p", "doc.md"), "one\n")
        vidx = os.path.join(root, "versions", "_index", time.strftime("%Y-%m-%d") + ".jsonl")
        fh = _hold_index(vidx)
        try:
            with set_attr(store, "LOCK_WAIT_S", 0.3):
                r = store.copy_version(g, root, st, "s", "Edit")
                rc, out = hook_inproc(edit_event(write(g, "two\n")))
        finally:
            _release_index(fh)
        t.check("lock: a version copy whose index is held -> the copy exists, not indexed, its record is in the orphans file",
                r["status"] == "ok" and not r["indexed"] and r["id"] == "v:orphans:1"
                and os.path.isfile(os.path.join(root, "versions", "_index", "_orphans.jsonl")), r)
        t.check("lock: ... and the hook says NOT INDEXED to the agent", "NOT INDEXED" in out, out[:200])
        hits, _p = store.find("doc.md", st)
        t.check("lock: find lists the orphan copy", any(h["id"].startswith("v:orphans:") for h in hits), hits)


# ---- 12. long paths in the hook (emulating LongPathsEnabled=0; this machine has it ON)

@contextlib.contextmanager
def no_long_paths():
    real_stat, real_lstat = os.stat, os.lstat

    def guard(fn):
        def w(p, *a, **k):
            if isinstance(p, str) and len(p) >= 260 and not p.startswith("\\\\?\\"):
                raise FileNotFoundError(2, "emulated: no long paths", p)
            return fn(p, *a, **k)
        return w

    os.stat, os.lstat = guard(real_stat), guard(real_lstat)
    try:
        yield
    finally:
        os.stat, os.lstat = real_stat, real_lstat


def t_hook_long_paths(t):
    sb = sandbox("hlong")
    with store_env(sb) as (root, st):
        deep = sb
        while len(deep) < 300:
            deep = os.path.join(deep, "segment_%02d_long_folder_name" % (len(deep) % 100))
        lf = os.path.join(deep, "long.txt")
        try:
            os.makedirs(store._lp(deep), exist_ok=True)
            with open(store._lp(lf), "w") as fh:
                fh.write("long\n")
        except OSError as e:
            raise Skip("the OS does not allow creating a path over 260: %s" % e)
        with no_long_paths():
            rc, out = hook_inproc(edit_event(lf))
            rc2, out2 = hook_inproc(bash_event('rm "%s"' % norm(lf), sb))
        idx = os.path.join(root, "versions", "_index", store_day() + ".jsonl")
        recs = lines(idx) if os.path.isfile(idx) else []
        t.check("long path (%d chars): the hook copies the file (was: 'not a file' -> no copy, allowed)" % len(lf),
                verdict(out) != "deny" and len(recs) == 1 and os.path.isfile(store._lp(recs[0]["copy"])), (out, recs))
        t.check("long path: a deletion of it is refused (was: 'deletes nothing' -> allowed)", verdict(out2) == "deny", out2[:200])


# ---- 13. the ready command is quoted for the shell it came from

def t_ready_command_quoting(t):
    q = getattr(pre_tool, "_q", None)
    t.check("quote: bash -- single quotes, ' becomes '\\''", q is not None and q("a'b$c", "bash") == "'a'\\''b$c'", "no _q")
    t.check("quote: PowerShell -- single quotes, ' becomes ''", q is not None and q("a'b", "powershell") == "'a''b'", "no _q")
    # the names come from a MASK (`price*`): a name found on disk is not screened for `$` or a backtick
    names = ["price$1.txt", "price's.txt", "price`tick.txt", "price plain.txt"]
    for tool, label in (("Bash", "bash"), ("PowerShell", "powershell")):
        runner = _bash() if tool == "Bash" else _powershell()
        if not runner:
            t.skip("quote: run the ready command in %s" % label, "no %s on this machine" % label)
            continue
        sb = sandbox("hq" + label)
        with store_env(sb) as (root, st):
            files = [write(os.path.join(sb, n), "content of %s\n" % n) for n in names]
            cmd = "rm price*" if tool == "Bash" else "Remove-Item price*"
            rc, out, err = hook_sub(PRE_TOOL, bash_event(cmd, sb, tool))
            obj = parse(out)
            reason = obj["hookSpecificOutput"]["permissionDecisionReason"] if obj else ""
            quoted = [pre_tool._q(norm(f), label) for f in files] if hasattr(pre_tool, "_q") else []
            t.check("quote [%s]: the deny names every file, single-quoted for this shell" % label,
                    verdict(out) == "deny" and quoted and all(q in reason for q in quoted), reason[:400])
            ready = reason.split("Run: ", 1)[1].split(" -- ")[0] if "Run: " in reason else ""
            argv = [runner, "-c", ready] if tool == "Bash" else [runner, "-NoProfile", "-Command", ready]
            p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=120, env=clean_env())
            t.check("quote [%s]: the ready command moves ALL four files (a `$` or a quote in a name breaks nothing)" % label,
                    ready and p.returncode == 0 and not any(os.path.exists(f) for f in files), (p.returncode, p.stdout[-200:], p.stderr[-200:]))
            hits, _p = store.find("price plain", st)
            t.check("quote [%s]: and they are in the trash, findable" % label, len(hits) == 1, hits)


# ---- 14. exit codes of `shedkeeper.py trash`

def shedkeeper_cli(*args):
    p = subprocess.run([PY, SHEDKEEPER_PY] + list(args), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60, env=clean_env())
    return p.returncode, p.stdout + p.stderr


def t_cli_trash_exit_codes(t):
    sb = sandbox("cli1")
    with store_env(sb) as (root, st):
        a = write(os.path.join(sb, "a.txt"))
        b = write(os.path.join(sb, "b.txt"))
        nope = os.path.join(sb, "nope.txt")
        rc, out = shedkeeper_cli("trash", nope, a)
        t.check("cli: a refused path first, then a moved one -> exit 1 and a summary line",
                rc == 1 and "summary" in out and not os.path.exists(a), (rc, out))
        rc, out = shedkeeper_cli("trash", b, nope)
        t.check("cli: a moved path first, then a refused one -> exit 1", rc == 1 and not os.path.exists(b), (rc, out))
    sb2 = sandbox("cli2")
    with store_env(sb2) as (root, st):
        os.makedirs(os.path.join(root, "trash", "_index", time.strftime("%Y-%m-%d") + ".jsonl"))   # index cannot be written
        c1 = write(os.path.join(sb2, "c1.txt"))
        c2 = write(os.path.join(sb2, "c2.txt"))
        nope = os.path.join(sb2, "nope.txt")
        rc, out = shedkeeper_cli("trash", c1, nope)
        t.check("cli: moved-but-not-recorded, then a refused path -> exit 1 (was 3: 'done, do not repeat')",
                rc == 1 and not os.path.exists(c1), (rc, out))
        rc, out = shedkeeper_cli("trash", c2)
        t.check("cli: moved-but-not-recorded and nothing refused -> exit 3", rc == 3 and not os.path.exists(c2), (rc, out))


# ---- 16. install check runs the hook; matcher; disableAllHooks

def t_install_check(t):
    sb = sandbox("icheck")
    home = os.path.join(sb, "home")
    os.makedirs(home)
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    sp = os.path.join(proj, ".claude", "settings.json")
    with env(SHEDKEEPER_USER_HOME=home, SHEDKEEPER_STORE_ROOT=os.path.join(sb, "store"), SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        rc, out = inst("install", "--project", proj)
        good = jload(sp)
        rc, out = inst("check", "--project", proj)
        probe_lines = [x for x in out.splitlines() if x.startswith("probe (")]
        t.check("check: a healthy install -> exit 0 and a probe line that says OK",
                rc == 0 and len(probe_lines) == 1 and ": OK -- " in probe_lines[0], out)

        def cloned():
            return json.loads(json.dumps(good))

        no_python = dict(clean_env(), PATH=os.path.join(WINDIR, "System32"))
        p = subprocess.run([PY, INSTALL, "check", "--project", proj], capture_output=True, text=True, encoding="utf-8",
                           timeout=120, env=no_python)
        rc, out = p.returncode, p.stdout + p.stderr
        t.check("check: hooks whose command does not start (no python on PATH) -> exit 1, the probe FAILED (was: 'python in PATH' and exit 0)",
                rc == 1 and any(x.startswith("probe (") and ": FAILED -- " in x for x in out.splitlines()), out)
        bad_matcher = cloned()
        bad_matcher["hooks"]["PreToolUse"][0]["matcher"] = "Write"
        write(sp, json.dumps(bad_matcher))
        rc, out = inst("check", "--project", proj)
        t.check("check: an edited matcher -> exit 1, the matcher is named", rc == 1 and "matcher" in out, out)
        rc, out = inst("install", "--project", proj)
        t.check("install: on an existing entry the matcher is put back", "matcher updated" in out
                and jload(sp)["hooks"]["PreToolUse"][0]["matcher"] == "Edit|Write|NotebookEdit|MultiEdit|Bash|PowerShell", out)
        rc, out = inst("check", "--project", proj)
        t.check("check: after that -> exit 0 again", rc == 0, out)
        off = cloned()
        off["disableAllHooks"] = True
        write(sp, json.dumps(off))
        rc, out = inst("check", "--project", proj)
        t.check("check: disableAllHooks = true -> exit 1 and it is said", rc == 1 and "disableAllHooks" in out, out)
        old_matcher = cloned()
        old_matcher["hooks"]["PreToolUse"][0]["matcher"] = "Edit|Write|NotebookEdit|Bash|PowerShell"
        write(sp, json.dumps(old_matcher))
        rc, out = inst("install", "--project", proj)
        t.check("install: a kit of the earlier version (no MultiEdit in the matcher) is brought up to date",
                "MultiEdit" in jload(sp)["hooks"]["PreToolUse"][0]["matcher"], out)


# ---- 17. MultiEdit, 18. .gitignore

def t_hook_multiedit(t):
    sb = sandbox("hmulti")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "p", "m.md"), "before\n")
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f, "MultiEdit"))
        t.check("hook: MultiEdit of an existing file -> a copy of the old content",
                verdict(out) != "deny" and len(copies_of(root, "m")) == 1 and read(copies_of(root, "m")[0]) == "before\n", (out, err[:120]))


def t_gitignore(t):
    p = os.path.join(KIT, ".gitignore")
    body = read(p) if os.path.isfile(p) else ""
    t.check("kit: .gitignore keeps __pycache__/ and *.pyc out", "__pycache__/" in body and "*.pyc" in body, body)


# =============================================================================================
# 5. mutations: a guarded place is broken in memory; the matching test must turn RED
# =============================================================================================

def run_mutation(t, name, patch_ctx, tests):
    """Break a guarded place in memory and run the tests that guard it. Outcomes: CAUGHT (an assertion
    turned red), SLIPPED (all green with the place broken), CRASH (a test raised: nothing was decided --
    NOT counted as caught)."""
    base = T(quiet=True)
    for fn in tests:
        base.run(fn)
    if any(r[0] == "NOT MEASURED" for r in base.rows):
        t._add("NOT MEASURED", "mutation %s" % name, "a test that guards it could not be measured here: nothing was decided")
        return
    if base.fails() or base.crashes():
        bad = (base.fails() or base.crashes())[0]
        t.check("mutation %s: the unbroken tests are green first" % name, False, "%s: %s" % (bad[1], bad[2]))
        return
    broken = T(quiet=True)
    with patch_ctx():
        for fn in tests:
            broken.run(fn)
    if broken.fails():
        t.check("mutation %s: CAUGHT" % name, True)
        say("        (red on: %s)" % broken.fails()[0][1][:100])
    elif broken.crashes():
        t.check("mutation %s: CRASH" % name, False,
                "the tests raised instead of failing (%s): nothing was decided" % broken.crashes()[0][2][:120])
    else:
        t.check("mutation %s: SLIPPED" % name, False, "the tests stayed green with the guarded place broken")


def t_mutations(t):
    def m_no_verify():
        return patched(store, "_verify_copy", lambda src, copy: (True, ""))

    def m_no_refusal():
        return patched(pre_tool, "_guard_copy", lambda res, path, settings: None)

    def m_word_search():
        def naive(command, cwd, shell="bash"):
            d = recognize.Deletion()
            if recognize.WORD_DEL.search(command):
                d.unresolved = True
            return d
        return patched(recognize, "delete_targets", naive)

    def m_overwrite_on_restore():
        return patched(store, "_occupied", lambda p: False)

    def m_substring_zone():
        return patched(store, "is_store_path", lambda path, settings: settings["store_dir_name"] in path.replace("\\", "/"))

    def m_no_trash_before_replace():
        return patched(store, "trash", lambda *a, **k: ("ok", "", {"dst": None}))

    # --- the guards added after a code review
    def m_stdin_ansi():                 # the hook reads its input the way `sys.stdin.read()` did: in the ANSI code page
        return patched(pre_tool, "_read_input", lambda: sys.stdin.buffer.read().decode("cp1251", "replace").encode("utf-8"))

    def m_mask_cut():                   # a mask is cut at the ceiling in silence
        real = recognize._expand
        return patched(recognize, "_expand", lambda p, cwd, limit=200, notes=None: real(p, cwd, 10 ** 9, None)[:200])

    def m_store_write_allowed():        # nothing is refused for a path inside the store
        return patched(store, "is_store_path", lambda path, settings: False)

    def m_no_rough():                   # a fault of the recognizer is always a pass
        return patched(pre_tool, "rough_destructive", lambda command: False)

    def m_no_lock():                    # the index is written without the lock
        return patched(store, "_lock", lambda fd, path: None)

    def m_no_long_prefix():             # the long-path form is not used
        return patched(store, "_lp", lambda p: os.path.abspath(p))

    def m_trust_creation():             # 'closed' is taken on trust from the creator, not READ from the DACL afterwards
        return patched(store, "check_private", lambda path, sid: (True, ""))

    def m_label_alone():                # a folder is recognised as ours by its LABEL alone (owner not read)
        def by_label(path, settings=None):
            state, rec = store.user_label_state(path)
            return state == "ours" and rec["sid"].upper() == store.identity()["sid"].upper(), ""
        return patched(store, "judge_personal", by_label)

    def m_owner_ignored():              # a folder is judged without reading its owner
        def no_owner_check(path, settings=None):
            state, rec = store.user_label_state(path)
            sid = store.identity()["sid"]
            if state != "ours" or rec["sid"].upper() != sid.upper():
                return False, "label"
            return True, ""
        return patched(store, "judge_personal", no_owner_check)

    def m_report_silent():              # the creation report says nothing
        return patched(store, "creation_report", lambda rep: "")

    def m_log_anywhere():               # the log writes into any folder
        return patched(store, "_is_mine", lambda root: True)

    def m_own_zone_only():              # is_store_path narrowed to the user's own personal folder
        return patched(store, "is_store_path",
                       lambda path, settings: store._within(path, store.store_root("x", settings) or "?:\\no_such_folder"))

    run_mutation(t, "sha256 comparison of the copy removed", m_no_verify, [t_version_verify])
    run_mutation(t, "refusal on a copy that was not taken removed", m_no_refusal, [t_hook_no_copy_denies])
    run_mutation(t, "deletion judged by a word anywhere instead of the head", m_word_search, [t_hook_mentions])
    run_mutation(t, "restore allowed to overwrite an occupied place", m_overwrite_on_restore, [t_restore_no_overwrite])
    run_mutation(t, "skip zone matched by substring instead of folder name", m_substring_zone, [t_store_zone, t_hook_zone])
    run_mutation(t, "version restore no longer moves the current file to the trash first", m_no_trash_before_replace, [t_restore_version])
    run_mutation(t, "hook input read in the ANSI code page instead of UTF-8 bytes", m_stdin_ansi, [t_hook_utf8_inproc])
    run_mutation(t, "a mask cut at the ceiling in silence", m_mask_cut, [t_hook_mask_ceiling])
    run_mutation(t, "a write into the store is allowed", m_store_write_allowed, [t_hook_store_write_denied])
    run_mutation(t, "a recognizer fault always passes the command", m_no_rough, [t_hook_recognizer_fault])
    run_mutation(t, "the index is written without the lock", m_no_lock, [t_index_lock_busy])
    run_mutation(t, "the long-path form of a path is not used", m_no_long_prefix, [t_hook_long_paths])
    run_mutation(t, "closed-ness taken on trust from the creator instead of read from the DACL", m_trust_creation,
                 [t_personal_permissions_refused])
    run_mutation(t, "a folder is recognised as ours by its label alone", m_label_alone, [t_personal_other_account])
    run_mutation(t, "a folder is judged without reading its owner", m_owner_ignored, [t_n2_numbered_names])
    run_mutation(t, "the report of a creation says nothing", m_report_silent, [t_n2_creation_report])
    run_mutation(t, "the log writes into a folder that is not ours", m_log_anywhere, [t_personal_other_account])
    run_mutation(t, "is_store_path narrowed to the own personal folder", m_own_zone_only, [t_personal_other_users_folder])
    # --- the personal store
    def m_open_then_closed():           # the temporary folder is born OPEN (os.mkdir) and closed afterwards
        real = store.make_dir_with_sddl
        sid = store.identity()["sid"]

        def open_then_close(path, sddl):
            if not os.path.basename(path).startswith("."):
                return real(path, sddl)
            os.mkdir(path)
            subprocess.run([os.path.join(WINDIR, "System32", "icacls.exe"), path, "/inheritance:r", "/grant:r",
                            "*%s:(OI)(CI)F" % sid, "*S-1-5-18:(OI)(CI)F"], capture_output=True, timeout=60)
        return patched(store, "make_dir_with_sddl", open_then_close)

    def m_sid_by_program():             # the SID is asked of an external program (whoami), as in 0.2.0
        def via_whoami():
            p = subprocess.run([os.path.join(WINDIR, "System32", "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                               capture_output=True, timeout=15)
            found = re.findall(r"S-1-\d+(?:-\d+)+", p.stdout.decode("ascii", "replace"))
            return (found[-1], "") if found else (None, "no SID in the output")
        return patched(store, "_lookup_sid", via_whoami)

    def m_extra_rights_new_name():      # a folder whose permissions carry an extra entry is 'not mine' (a new .2 is made)
        real = store.judge_personal

        def strict(path, settings=None):
            ok, why = real(path, settings)
            if ok and (settings or {}).get("private_store", True):
                ok2, why2 = store.check_private(path, store.identity()["sid"])
                if not ok2:
                    return False, why2
            return ok, why
        return patched(store, "judge_personal", strict)

    def m_find_silent():                # find / restore are not told what was not searched
        return patched(store, "unsearched", lambda settings: [])

    run_mutation(t, "the temporary folder is born open and closed afterwards", m_open_then_closed, [t_n3_created_closed])
    run_mutation(t, "the SID is asked of an external program", m_sid_by_program, [t_n3_sid_from_token])
    run_mutation(t, "an extra entry in the permissions makes the folder 'not mine' (a new number)", m_extra_rights_new_name,
                 [t_n3_extra_rights_stay_mine])
    run_mutation(t, "find / restore are silent about what they did not search", m_find_silent, [t_n3_find_not_searched])


def t_review_junction_into_store(t):
    """A junction elsewhere that points INTO a store is inside the store (independent review, finding 4)."""
    sb = sandbox("rev_junction")
    st = settings()
    with env(SHEDKEEPER_STORE_ROOT=None):
        real_store = os.path.join(sb, "vol", st["store_dir_name"])
        write(os.path.join(real_store, "trash", "x.txt"), "held\n")
        link = os.path.join(sb, "project", "store-link")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        try:
            p = subprocess.run(["cmd", "/c", "mklink", "/J", link, real_store], capture_output=True,
                               text=True, timeout=20)
            made = p.returncode == 0 and os.path.exists(link)
            why = (p.stdout + p.stderr).strip()[:120]
        except Exception as e:
            made, why = False, str(e)
        if not made:
            t.skip("review: a junction into the store is inside the store", "mklink /J not available: %s" % why)
            return
        t.check("review: a junction into the store is inside the store",
                store.is_store_path(os.path.join(link, "trash", "x.txt"), st))
        t.check("review: an ordinary file next to the junction is not",
                not store.is_store_path(os.path.join(sb, "project", "other.txt"), st))


def t_review_is_ours_boundary(t):
    """install.is_ours takes only the kit's own command (independent review, finding 5)."""
    import install as install_mod
    own = install_mod.hook_command("pre_tool.py")
    t.check("review: the kit's own hook command is ours", install_mod.is_ours(own))
    t.check("review: a command naming pre_tool.py.backup is NOT ours", not install_mod.is_ours(own[:-1] + '.backup"'))
    t.check("review: someone's wrapper that passes our path as an argument is NOT ours",
            not install_mod.is_ours(own + " --log x.txt"))


# =============================================================================================
# 8. the personal folder (0.2.0): each test here is red on the 0.1.0 kit (a guard on a behaviour that did
#    not change says so in its docstring)
# =============================================================================================

WINDIR = os.environ.get("SystemRoot") or "C:\\Windows"
USER_LABEL_NAME = ".shedkeeper-user"
FOREIGN_SID = "S-1-5-21-1-2-3-4"


def real_login():
    """The login name by a route that does not go through the kit (GetUserNameW)."""
    import ctypes
    buf = ctypes.create_unicode_buffer(257)
    n = ctypes.c_ulong(257)
    ctypes.windll.advapi32.GetUserNameW(buf, ctypes.byref(n))
    return buf.value


def real_sid():
    import re
    out = subprocess.run([os.path.join(WINDIR, "System32", "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                         capture_output=True, timeout=30).stdout.decode("ascii", "replace")
    return re.findall(r"S-1-\d+(?:-\d+)+", out)[-1]


def icacls_text(path):
    p = subprocess.run([os.path.join(WINDIR, "System32", "icacls.exe"), path], capture_output=True, timeout=60)
    return p.stdout.decode("oem", "replace")


def listing(parent):
    return sorted(os.listdir(parent)) if os.path.isdir(parent) else []


def tree(path):
    """[(relative path, size or None)] of everything under `path`: what 'nothing was touched' compares."""
    out = []
    for dp, dns, fns in os.walk(path):
        for n in dns:
            out.append((os.path.relpath(os.path.join(dp, n), path), None))
        for n in fns:
            out.append((os.path.relpath(os.path.join(dp, n), path), os.path.getsize(os.path.join(dp, n))))
    return sorted(out)


def forget_identity():
    getattr(store, "reset_identity", lambda: None)()


def t_personal_layout(t):
    sb = sandbox("pl")
    vol = os.path.join(sb, "vol")
    login, sid = real_login(), real_sid()
    pers = os.path.join(vol, login)
    f = write(os.path.join(sb, "p", "a.txt"), "hello\n")
    g = write(os.path.join(sb, "p", "b.txt"), "to the trash\n")
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    tp = write(os.path.join(sb, "cc", "proj-folder", "s1.jsonl"), "1\n")
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
        t.check("personal: the first Edit is allowed and the personal folder <volume>\\<login> is made",
                rc == 0 and verdict(out) != "deny" and os.path.isdir(pers), (rc, out, err[:160]))
        t.check("personal: the copy of the file is in the personal folder",
                len(versions_of(pers, "a.v_")) == 1 and read(versions_of(pers, "a.v_")[0]) == "hello\n", listing(vol))
        t.check("personal: the volume folder holds only its label and the personal folder",
                listing(vol) == sorted([".shedkeeper-store", login]), listing(vol))
        t.check("personal: no temporary folder is left behind", not [n for n in listing(vol) if ".part-" in n], listing(vol))
        lp = os.path.join(pers, USER_LABEL_NAME)
        rec = jload(lp) if os.path.isfile(lp) else {}
        t.check("personal: .shedkeeper-user holds product, sid, login, full_name, created",
                rec.get("product") == "shedkeeper" and rec.get("sid") == sid and rec.get("login") == login
                and "full_name" in rec and rec.get("created"), rec)
        t.check("personal: the volume folder keeps its label", store.label_state(vol)[0] == "ours")
        rc, out, err = hook_sub(PRE_TOOL, bash_event("rm a.txt", os.path.dirname(f)))
        t.check("personal: the log of the hook is in the personal folder, not in the volume folder",
                os.path.isfile(os.path.join(pers, "log.jsonl")) and not os.path.exists(os.path.join(vol, "log.jsonl")), listing(vol))
        ev = {"hook_event_name": "SessionEnd", "transcript_path": tp, "session_id": "s1", "cwd": proj}
        rc, out, err = hook_sub(SESSION, ev)
        t.check("personal: the transcript copy is in the personal folder",
                os.path.isfile(os.path.join(pers, "transcripts", "proj-folder", "s1.jsonl")), (rc, out, err[:120]))
        rc, out = shedkeeper_cli("trash", g, "--reason", "layout test")
        t.check("personal: a trashed file lies under <volume>\\<login>\\trash, nothing under <volume>\\trash",
                rc == 0 and not os.path.exists(g) and not os.path.exists(os.path.join(vol, "trash"))
                and any("b.txt" in fs for _dp, _d, fs in os.walk(os.path.join(pers, "trash"))), (rc, out, listing(vol)))
        rc, out = shedkeeper_cli("find", "b.txt")
        low = out.replace("\\", "/").lower()
        t.check("personal: find lists it, at a place inside the personal folder",
                rc == 0 and (norm(pers).lower() + "/trash/") in low, out)
        import re
        m = re.search(r"t:\d{4}-\d{2}-\d{2}:\d+", out)
        rc, out2 = shedkeeper_cli("restore", m.group(0)) if m else (1, "no id in: " + out)
        t.check("personal: restore from the new layout brings it back", rc == 0 and read(g) == "to the trash\n", (rc, out2))
        write(f, "changed\n")
        rc, out = shedkeeper_cli("find", "a.txt")
        mv = re.search(r"v:\d{4}-\d{2}-\d{2}:\d+", out)
        rc, out2 = shedkeeper_cli("restore", mv.group(0)) if mv else (1, "no id in: " + out)
        t.check("personal: restore of an earlier version from the new layout works",
                rc == 0 and read(f) == "hello\n" and (norm(pers).lower() + "/versions/") in out.replace("\\", "/").lower(), (rc, out, out2))
        ok, note = store.ensure_store(pers, settings())
        t.check("personal: a second ensure_store changes nothing and says nothing", ok and note == "", (ok, note))


def t_personal_permissions_read(t):
    sb = sandbox("pp")
    vol = os.path.join(sb, "vol")
    login, sid = real_login(), real_sid()
    pers = os.path.join(vol, login)
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        ok, note = store.ensure_store(pers, settings())
        t.check("permissions: the personal folder is made", ok and os.path.isdir(pers), (ok, note))
        txt = icacls_text(pers)
        t.check("permissions: icacls (an independent reader) shows two full-control entries and no inherited one",
                txt.count("(F)") == 2 and txt.count("(I)") == 0 and login.lower() in txt.lower(), txt)
        rd = getattr(store, "read_dacl", None)
        prot, aces = rd(pers) if rd else (None, None)
        t.check("permissions: read from the DACL -- protected, exactly two ACCESS_ALLOWED: the user and SYSTEM",
                prot is True and aces is not None and len(aces) == 2 and all(a[0] == 0 for a in aces)
                and sorted(a[3] for a in aces) == sorted([sid, "S-1-5-18"]), (prot, aces))
        cp = getattr(store, "check_private", None)
        t.check("permissions: check_private accepts it", cp is not None and cp(pers, sid)[0], cp(pers, sid) if cp else "no check_private")
        plain = os.path.join(sb, "plain")
        os.makedirs(plain)
        t.check("permissions: control -- a folder that inherits its permissions is NOT private",
                cp is not None and not cp(plain, sid)[0] and "inherit" in cp(plain, sid)[1], cp(plain, sid) if cp else "no check_private")
        three = os.path.join(sb, "three")
        os.makedirs(three)
        subprocess.run([os.path.join(WINDIR, "System32", "icacls.exe"), three, "/inheritance:r", "/grant:r",
                        "*%s:(OI)(CI)F" % sid, "*S-1-5-18:(OI)(CI)F", "*S-1-1-0:(OI)(CI)R"], capture_output=True, timeout=60)
        t.check("permissions: control -- protected but with a third entry (Everyone) is NOT private",
                cp is not None and not cp(three, sid)[0] and "S-1-1-0" in cp(three, sid)[1], cp(three, sid) if cp else "no check_private")
        t.check("permissions: control -- the right entries for ANOTHER user are NOT private for this one",
                cp is not None and not cp(pers, FOREIGN_SID)[0], cp(pers, FOREIGN_SID) if cp else "no check_private")
        f = write(os.path.join(sb, "p", "a.txt"), "x\n")
        st, msg, info = store.trash(f, "", settings(), "", NOW)
        t.check("permissions: what is trashed keeps ITS OWN permissions (a rename, not a copy): it stays readable "
                "by the entries it had", st == "ok" and os.path.isfile(info["dst"]) and icacls_text(info["dst"]).count("(F)") >= 1, (st, msg))


def t_personal_permissions_refused(t):
    """The permissions did not take (icacls refused / did nothing / the label failed): no store, the action
    refused, and the temporary folder is removed only when empty."""
    sb = sandbox("pr")
    vol = os.path.join(sb, "vol")
    pers = os.path.join(vol, real_login())
    f = write(os.path.join(sb, "p", "a.txt"), "keep\n")
    st = settings()

    real_make = getattr(store, "make_dir_with_sddl", None)

    def messy(path, sddl):                  # the creator makes the folder and somebody puts a file into it
        real_make(path, sddl)
        if os.path.basename(path).startswith("."):
            write(os.path.join(path, "junk.txt"), "j")

    def label_fails(root, ident):
        raise OSError(28, "No space left on device")

    def create_fails(path, sddl):
        if os.path.basename(path).startswith("."):
            raise OSError(5, "Access is denied.")
        return real_make(path, sddl)

    def create_open(path, sddl):            # the creator hands over an OPEN folder (inherited permissions): the DACL read decides
        if os.path.basename(path).startswith("."):
            return os.mkdir(path)
        return real_make(path, sddl)

    variants = [("creation with the descriptor fails (access denied)", "make_dir_with_sddl", create_fails, "NOT made"),
                ("the folder comes out open (the DACL read decides, not the creator)", "make_dir_with_sddl", create_open, "inherits"),
                ("the label cannot be written", "_write_user_label", label_fails, "label")]
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        for label, name, fake, needle in variants:
            with set_attr(store, name, fake):
                ok, why = store.ensure_store(pers, st)
                t.check("refused [%s]: no store is made" % label, not ok and not os.path.exists(pers), (ok, why))
                t.check("refused [%s]: the reason is named" % label, needle in why, why)
                t.check("refused [%s]: the empty temporary folder is removed" % label,
                        not [n for n in listing(vol) if ".part-" in n], listing(vol))
                rc, out = hook_inproc(edit_event(f))
                t.check("refused [%s]: an Edit of an existing file is DENIED" % label, verdict(out) == "deny", out[:200])
                s2, msg, _i = store.trash(f, "", st, "", NOW)
                t.check("refused [%s]: trash is refused and the file stays" % label,
                        s2 == "refused" and os.path.isfile(f) and not os.path.exists(pers), (s2, msg))
    vol2 = os.path.join(sb, "vol2")
    pers2 = os.path.join(vol2, real_login())
    with env(SHEDKEEPER_STORE_ROOT=vol2, SHEDKEEPER_INI=os.path.join(sb, "no.ini")), set_attr(store, "make_dir_with_sddl", messy):
        ok, why = store.ensure_store(pers2, st)
        left = [n for n in listing(vol2) if ".part-" in n]
        t.check("refused: a temporary folder that is NOT empty is left and NAMED in the text (never emptied)",
                not ok and len(left) == 1 and left[0] in why and os.path.isfile(os.path.join(vol2, left[0], "junk.txt"))
                and not os.path.exists(pers2), (ok, why, left))


def t_personal_other_account(t):
    """The agent guide: a folder with the name of this user that is NOT this user's store (another account's SID in
    the label, no label, a damaged label) is skipped and left exactly as it is; the store is made under the
    next free name (<login>.2). Only when every name up to the ceiling is taken is the use refused."""
    sb = sandbox("po")
    login = real_login()
    f = write(os.path.join(sb, "p", "a.txt"), "keep\n")
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    tp = write(os.path.join(sb, "cc", "proj-folder", "s1.jsonl"), "1\n")

    def prep(name, label):
        vol = os.path.join(sb, name)
        write(os.path.join(vol, ".shedkeeper-store"), json.dumps({"product": "shedkeeper", "created": "x"}))
        pers = os.path.join(vol, login)
        os.makedirs(pers)
        if label is not None:
            write(os.path.join(pers, USER_LABEL_NAME), label)
        return vol, pers

    cases = [("other", json.dumps({"product": "shedkeeper", "sid": FOREIGN_SID, "login": login, "full_name": "",
                                   "created": "x"}), "another SID"),
             ("nolabel", None, "no " + USER_LABEL_NAME + " label"),
             ("badlabel", "{ not json", "unreadable")]
    for name, label, needle in cases:
        vol, pers = prep(name, label)
        second = os.path.join(vol, login + ".2")
        with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
            before = tree(pers)
            rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
            t.check("[%s] the hook ALLOWS the Edit: the store is made under the next name %s.2" % (name, login),
                    rc == 0 and verdict(out) != "deny" and os.path.isdir(second) and len(versions_of(second, "a.v_")) == 1,
                    (rc, out[:300], err[:120], listing(vol)))
            t.check("[%s] the folder that is not ours is exactly as it was (files, sizes, no log line)" % name,
                    tree(pers) == before, (before, tree(pers)))
            t.check("[%s] no temporary folder is left" % name, not [n for n in listing(vol) if ".part-" in n], listing(vol))
            t.check("[%s] store_root names the numbered folder, all_roots lists only it" % name,
                    norm(store.store_root("x", settings())) == norm(second) and [norm(r) for r in store.all_roots(settings())] == [norm(second)],
                    (store.store_root("x", settings()), store.all_roots(settings())))
            store.log(pers, {"event": "probe"})
            t.check("[%s] even the log does not write into the folder that is not ours" % name, tree(pers) == before, tree(pers))
            rc, out = shedkeeper_cli("trash", f, "--reason", "n2")
            t.check("[%s] the CLI trashes into the numbered folder, the other one stays untouched" % name,
                    rc == 0 and not os.path.exists(f) and tree(pers) == before
                    and any("a.txt" in fs for _dp, _d, fs in os.walk(os.path.join(second, "trash"))), (rc, out))
            write(f, "keep\n")
            rc, out = shedkeeper_cli("status")
            t.check("[%s] status says why the other folder is skipped" % name,
                    ("skipped") in out and needle in out and norm(pers).lower() in out.replace("\\", "/").lower(), out)
            if name == "other":
                ev = {"hook_event_name": "SessionStart", "transcript_path": tp, "session_id": "s1", "cwd": proj}
                rc, out, err = hook_sub(SESSION, ev)
                t.check("[other] the session hook writes the transcript into the numbered folder, not the other one",
                        rc == 0 and os.path.isfile(os.path.join(second, "transcripts", "proj-folder", "s1.jsonl"))
                        and not os.path.exists(os.path.join(pers, "transcripts")), (rc, out[:200]))
    # --- the ceiling: every name up to <login>.99 is taken by something that is not ours -> refusal, nothing touched
    vol = os.path.join(sb, "full")
    write(os.path.join(vol, ".shedkeeper-store"), json.dumps({"product": "shedkeeper", "created": "x"}))
    for nm in store.personal_names(login):
        os.makedirs(os.path.join(vol, nm))
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        before = tree(vol)
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
        t.check("[ceiling] all %d names taken: the hook DENIES the Edit and says why" % len(store.personal_names(login)),
                rc == 0 and verdict(out) == "deny" and "every name" in out, (rc, out[:300], err[:120]))
        rc, out = shedkeeper_cli("trash", f)
        t.check("[ceiling] the CLI refuses (exit 1), the file stays", rc == 1 and os.path.isfile(f) and "every name" in out, (rc, out))
        t.check("[ceiling] nothing was touched", tree(vol) == before, "tree changed")
    # --- a label with my SID in a folder I own whose permissions are inherited: still MINE (the permissions do not
    #     decide; they are reported), and the hook goes on using it
    vol = os.path.join(sb, "forged")
    write(os.path.join(vol, ".shedkeeper-store"), json.dumps({"product": "shedkeeper", "created": "x"}))
    pers = os.path.join(vol, login)
    write(os.path.join(pers, USER_LABEL_NAME), json.dumps({"product": "shedkeeper", "sid": real_sid(), "login": login,
                                                           "full_name": "", "created": "x"}))
    second = os.path.join(vol, login + ".2")
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        ok, why = store.judge_personal(pers, settings())
        t.check("[open] a folder of mine (owner me, label with my SID) whose permissions are inherited STAYS mine", ok, (ok, why))
        priv, text = store.private_state(pers, settings())
        t.check("[open] ... and private_state says NO with the reason", not priv and "inherits" in text, (priv, text))
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
        t.check("[open] the hook keeps using it: the copy is in it, no %s.2 is made" % login,
                rc == 0 and verdict(out) != "deny" and not os.path.exists(second) and len(versions_of(pers, "a.v_")) == 1,
                (rc, out[:300], listing(vol)))
    # --- the OWNER is somebody else: a label with my SID in such a folder is forged for real -> NOT mine
    vol = os.path.join(sb, "forged2")
    write(os.path.join(vol, ".shedkeeper-store"), json.dumps({"product": "shedkeeper", "created": "x"}))
    pers = os.path.join(vol, login)
    write(os.path.join(pers, USER_LABEL_NAME), json.dumps({"product": "shedkeeper", "sid": real_sid(), "login": login,
                                                           "full_name": "", "created": "x"}))
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")), patched(store, "read_owner", lambda p: FOREIGN_SID):
        ok, why = store.judge_personal(pers, settings())
        t.check("[forged] a label with my SID in a folder whose OWNER is somebody else is NOT mine", not ok and "owner" in why, (ok, why))
        t.check("[forged] the store is then the next name, and the forged folder is not listed",
                norm(store.store_root("x", settings())) == norm(os.path.join(vol, login + ".2"))
                and os.path.join(vol, login) not in store.all_roots(settings()), (store.store_root("x", settings()), store.all_roots(settings())))


def t_personal_private_false_and_status(t):
    sb = sandbox("pf")
    login, sid = real_login(), real_sid()
    f = write(os.path.join(sb, "p", "a.txt"), "x\n")
    home = os.path.join(sb, "home")
    os.makedirs(home)
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    volf, volt = os.path.join(sb, "volf"), os.path.join(sb, "volt")
    ini_f = write(os.path.join(sb, "f.ini"), "private_store = false\n")
    ini_t = write(os.path.join(sb, "t.ini"), "private_store = true\n")
    with env(SHEDKEEPER_STORE_ROOT=volf, SHEDKEEPER_INI=ini_f):
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
        pers = os.path.join(volf, login)
        t.check("private_store = false: the personal folder is made and the copy taken",
                os.path.isdir(pers) and len(versions_of(pers, "a.v_")) == 1, (rc, out[:160], err[:120]))
        t.check("private_store = false: the permissions are NOT changed (inherited entries, nothing protected)",
                os.path.isdir(pers) and "(I)" in icacls_text(pers), icacls_text(pers) if os.path.isdir(pers) else "no folder")
        lp = os.path.join(pers, USER_LABEL_NAME)
        t.check("private_store = false: the label is the same", os.path.isfile(lp) and jload(lp).get("sid") == sid, lp)
        rc, out = shedkeeper_cli("status")
        t.check("private_store = false: status says `private: no (private_store = false)` and names the folder",
                "private: no (private_store = false)" in out and norm(pers).lower() in out.replace("\\", "/").lower(), out)
    with env(SHEDKEEPER_STORE_ROOT=volt, SHEDKEEPER_INI=ini_t, SHEDKEEPER_USER_HOME=home):
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
        pers_t = os.path.join(volt, login)
        rc, out = shedkeeper_cli("status")
        t.check("private_store = true: status says `store of <login> ... -- private: yes`",
                ("store of %s" % login) in out and "-- private: yes" in out and norm(pers_t).lower() in out.replace("\\", "/").lower(), out)
        rc, out = inst("install", "--project", proj)
        rc, out = inst("check", "--project", proj)
        t.check("private_store = true: install check exits 0 and says private: yes", rc == 0 and "-- private: yes" in out, (rc, out))
    s, problems = config.load(write(os.path.join(sb, "x.ini"), "private_store = perhaps\n"))
    t.check("config: private_store not understood -> true (the safe side), and it is said",
            s.get("private_store") is True and any("private_store" in x and "true" in x for x in problems), (s.get("private_store"), problems))
    s, problems = config.load(write(os.path.join(sb, "y.ini"), "private_store = no\n"))
    t.check("config: private_store = no is honoured", s.get("private_store") is False and not problems, (s.get("private_store"), problems))
    t.check("config: the default is true", config.DEFAULTS.get("private_store") is True, config.DEFAULTS)


def t_personal_open_folder_is_named(t):
    """The personal store: with private_store = true a folder of this user that is NOT closed (made earlier with
    private_store = false) STAYS the store -- no <login>.2, its copies stay findable -- and is reported aloud:
    `status` says `private: NO` with the reason, `install check` fails. With private_store = false the same folder
    is accepted too (owner is me)."""
    sb = sandbox("pn")
    login = real_login()
    f = write(os.path.join(sb, "p", "a.txt"), "x\n")
    f2 = write(os.path.join(sb, "p", "b.txt"), "y\n")
    home = os.path.join(sb, "home")
    os.makedirs(home)
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    vol = os.path.join(sb, "vol")
    ini_f = write(os.path.join(sb, "f.ini"), "private_store = false\n")
    ini_t = write(os.path.join(sb, "t.ini"), "private_store = true\n")
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=ini_f, SHEDKEEPER_USER_HOME=home):
        hook_sub(PRE_TOOL, edit_event(f))
    first = os.path.join(vol, login)
    second = os.path.join(vol, login + ".2")
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=ini_f, SHEDKEEPER_USER_HOME=home):
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f2))
        t.check("open folder, private_store = false: the same folder is still the store (no new number)",
                verdict(out) != "deny" and len(versions_of(first, "b.v_")) == 1 and not os.path.exists(second), (out[:200], err[:120]))
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=ini_t, SHEDKEEPER_USER_HOME=home):
        before, dacl = tree(first), store.read_dacl(first)
        write(f2, "y changed\n")
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f2))
        t.check("open folder, private_store = true: it STAYS the store -- the copy goes into it, no %s.2 is made" % login,
                verdict(out) != "deny" and len(versions_of(first, "b.v_")) == 2 and not os.path.exists(second), (out[:200], err[:120], listing(vol)))
        t.check("open folder, private_store = true: the tool did not touch its permissions", store.read_dacl(first) == dacl, store.read_dacl(first))
        rc, out = shedkeeper_cli("status")
        t.check("open folder: status says `private: NO` with the reason (inherits), and names no skipped folder",
                "-- private: NO" in out and "inherit" in out and "skipped" not in out, out)
        inst("install", "--project", proj)
        rc, out = inst("check", "--project", proj)
        t.check("open folder: install check FAILS (exit 1) and says private: NO -- a store meant to be closed and not closed",
                rc == 1 and "-- private: NO" in out, (rc, out))


def t_personal_identity(t):
    sb = sandbox("pi")
    login, sid = real_login(), real_sid()
    f = write(os.path.join(sb, "p", "a.txt"), "x\n")
    vol = os.path.join(sb, "vol")
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f), env_extra={"USERNAME": "Mallory", "USER": "Mallory"})
        t.check("identity: the folder is named by the login of the system (GetUserNameW), not by USERNAME",
                os.path.isdir(os.path.join(vol, login)) and not os.path.exists(os.path.join(vol, "Mallory")), (listing(vol), out[:160], err[:120]))
    # no program is started to find out who I am (the personal store: the SID comes from the token of the process)
    vol2 = os.path.join(sb, "vol2")
    forget_identity()
    calls, fake = spawn_spy()
    try:
        with env(SHEDKEEPER_STORE_ROOT=vol2, SHEDKEEPER_INI=os.path.join(sb, "no.ini")), patched(subprocess, "run", fake), \
                patched(subprocess, "Popen", fake):
            ok2, note2 = store.ensure_store(os.path.join(vol2, login), settings())
    finally:
        forget_identity()
    t.check("identity: making a personal folder starts NO program (no whoami, no icacls: the SID comes from the process token)",
            ok2 and not calls, (ok2, note2, calls))
    got = store._lookup_sid()
    t.check("identity: the SID from the token is the SID that whoami reports", got == (sid, ""), got)

    def token_denied():
        raise OSError(5, "token denied")
    with set_attr(store, "_token_user_sid", token_denied):
        got = store._lookup_sid()
    t.check("identity: a token that cannot be read -> no SID, a reason, no exception", got[0] is None and got[1], got)
    with set_attr(store, "_token_user_sid", lambda: "not-a-sid"):
        got = store._lookup_sid()
    t.check("identity: an answer that is not a SID -> no SID, a reason", got[0] is None and got[1], got)
    # no SID -> no personal folder
    vol3 = os.path.join(sb, "vol3")
    root3 = os.path.join(vol3, login)
    forget_identity()
    try:
        with env(SHEDKEEPER_STORE_ROOT=vol3, SHEDKEEPER_INI=os.path.join(sb, "no.ini")), \
                set_attr(store, "_lookup_sid", lambda: (None, "whoami is broken")):
            forget_identity()
            ok, why = store.check_available(root3, settings())
            t.check("identity: no SID -> check_available refuses and names the SID", not ok and "SID" in why, (ok, why))
            ok, why = store.ensure_store(root3, settings())
            t.check("identity: no SID -> ensure_store refuses, nothing is created", not ok and not os.path.exists(root3), (ok, why))
            rc, out = hook_inproc(edit_event(f))
            t.check("identity: no SID -> an Edit of an existing file is DENIED", verdict(out) == "deny", out[:200])
    finally:
        forget_identity()
    fn = getattr(store, "_full_name", None)
    t.check("identity: a full name that cannot be had is '' (not an error)",
            fn is not None and fn("NoSuchAccount_zz9") == "", fn("NoSuchAccount_zz9") if fn else "no _full_name")
    lp = os.path.join(vol, login, USER_LABEL_NAME)
    t.check("identity: the label carries the full name of the account (or '')",
            os.path.isfile(lp) and jload(lp).get("full_name") == store._full_name(login), (lp, store._full_name(login)))


def t_personal_other_users_folder(t):
    """Guard on a behaviour that did NOT change (so it is green on 0.1.0 as well): is_store_path is not
    narrowed -- any path with a `_shedkeeper` component is inside the store, another user's personal folder
    included. The mutation `own folder only` turns it red."""
    sb = sandbox("pu")
    vol = os.path.join(sb, "vol")
    st = settings()
    with env(SHEDKEEPER_STORE_ROOT=None):
        for path in ("D:/_shedkeeper/SomeoneElse/trash/a.txt", "D:/_shedkeeper/SomeoneElse/versions/x/y.txt",
                     "D:/_shedkeeper/SomeoneElse/log.jsonl", "D:/proj/_shedkeeper/SomeoneElse/.shedkeeper-user"):
            t.check("other user's folder: %s is inside the store" % path, store.is_store_path(path, st), path)
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        theirs = write(os.path.join(vol, "SomeoneElse", "trash", "x.txt"), "theirs\n")
        rc, out = hook_inproc(edit_event(theirs))
        t.check("other user's folder: an Edit of a file in it is refused (a person changes the store)",
                verdict(out) == "deny" and "person" in out, out[:200])
        rc, out = hook_inproc(bash_event('echo x > "%s"' % norm(theirs), sb))
        t.check("other user's folder: a redirect into it is refused", verdict(out) == "deny" and "person" in out, out[:200])
        rc, out = hook_inproc(bash_event("rm %s" % norm(theirs), sb))
        t.check("other user's folder: a deletion in it is refused", verdict(out) == "deny" and "person" in out, out[:200])
        s2, msg, _i = store.trash(theirs, "", st, "", NOW)
        t.check("other user's folder: trash of a file in it is refused", s2 == "refused" and os.path.isfile(theirs), (s2, msg))
        t.check("other user's folder: its content is intact", read(theirs) == "theirs\n")


def t_personal_docs_and_version(t):
    readme = read(os.path.join(KIT, "README.md"))
    ini = read(os.path.join(KIT, "shedkeeper.ini"))
    t.check("version: VERSION is 0.2.0", read(os.path.join(KIT, "VERSION")).strip() == "0.2.0", read(os.path.join(KIT, "VERSION")))
    t.check("README: the layout names the personal folder and its label",
            "<login name>" in readme and ".shedkeeper-user" in readme, "no personal folder in the layout")
    t.check("README: private_store is documented", "private_store" in readme)
    t.check("README: the Windows recycle bin comparison", "$Recycle.Bin" in readme)
    low = readme.lower()
    t.check("README: an administrator can take any folder; a trashed item keeps its permissions",
            "administrator" in low and "keeps its" in low and "permissions" in low, "missing")
    t.check("README: the old line that the store is open to other users is gone", "may be able to read the" not in readme)
    t.check("shedkeeper.ini: private_store = true is there, with a comment", "private_store = true" in ini and "#" in ini.split("private_store = true")[0].splitlines()[-1])


# =============================================================================================
# 9. the agent guide: the way back through the agent, a taken name -> a number, the report of a creation
# =============================================================================================

def one_json(out):
    """The hook output must be ONE line of valid JSON (or nothing). -> the object, or None."""
    if not out.strip():
        return {}
    if "\n" in out.strip():
        return None
    try:
        obj = json.loads(out)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def vol_with_label(sb, name):
    vol = os.path.join(sb, name)
    write(os.path.join(vol, ".shedkeeper-store"), json.dumps({"product": "shedkeeper", "created": "x"}))
    return vol


def t_n2_guide_at_session_start(t):
    sb = sandbox("n2g")
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    tp = write(os.path.join(sb, "cc", "proj-folder", "s1.jsonl"), "1\n")
    with env(SHEDKEEPER_STORE_ROOT=os.path.join(sb, "vol"), SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        ev = {"hook_event_name": "SessionStart", "transcript_path": tp, "session_id": "s1", "cwd": proj}
        rc, out, err = hook_sub(SESSION, ev)
        obj = one_json(out)
        ctx = (obj or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        guide, _sep, warn = ctx.partition(" WARNING: ")
        t.check("guide: SessionStart ALWAYS gives the agent the guide, one JSON line (the hooks are not installed here)",
                rc == 0 and obj is not None and ctx.startswith("shedkeeper: ") and "not found" in warn, (rc, out[:300], err[:120]))
        t.check("guide: it is at most 400 characters (the warning comes after, in the same line)", 0 < len(guide) <= 400, len(guide))
        t.check("guide: it names the real path of this kit (once, as K), find and restore",
                ('K="%s"' % norm(KIT)) in guide and "K/shedkeeper.py find " in guide and "restore <id>" in guide, guide)
        low = guide.lower()
        t.check("guide: restore only after the person agrees; explain first (what, when, why)",
                "only after the person agrees" in low and "explain first: what, when, why" in low, guide)
        ev["hook_event_name"] = "SessionEnd"
        rc, out, err = hook_sub(SESSION, ev)
        t.check("guide: SessionEnd prints no guide (it is not an event that has context)", rc == 0 and "additionalContext" not in out, out[:200])


def t_n2_find_gives_what_an_agent_needs(t):
    sb = sandbox("n2f")
    f = write(os.path.join(sb, "p", "a.txt"), "v1\n")
    g = write(os.path.join(sb, "p", "b.txt"), "to the trash\n")
    with env(SHEDKEEPER_STORE_ROOT=os.path.join(sb, "vol"), SHEDKEEPER_INI=os.path.join(sb, "no.ini"),
             CLAUDE_CODE_SESSION_ID="sess-n2-trash"):
        rc, out = shedkeeper_cli("trash", g, "--reason", "cleanup of the draft")
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
        rc, out = shedkeeper_cli("find", "b.txt")
        t.check("find: a trash hit prints the time, the reason and the session",
                rc == 0 and "cleanup of the draft" in out and "sess-n2-trash" in out and re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", out), out)
        rc, out = shedkeeper_cli("find", "a.txt")
        t.check("find: a version hit prints the time, how it was changed (the tool) and the session",
                rc == 0 and "via Edit" in out and "session s1" in out and re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", out), out)


def t_n2_numbered_names(t):
    sb = sandbox("n2n")
    login = real_login()
    f = write(os.path.join(sb, "p", "a.txt"), "x\n")
    f2 = write(os.path.join(sb, "p", "b.txt"), "y\n")
    vol = vol_with_label(sb, "vol")
    for nm in (login, login + ".2"):                                  # two folders that are not ours
        write(os.path.join(vol, nm, USER_LABEL_NAME), json.dumps({"product": "shedkeeper", "sid": FOREIGN_SID,
                                                                   "login": login, "full_name": "", "created": "x"}))
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        before = tree(vol)
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
        third = os.path.join(vol, login + ".3")
        t.check("numbered: two taken names -> the store is %s.3" % login, os.path.isdir(third)
                and len(versions_of(third, "a.v_")) == 1, (out[:200], listing(vol)))
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f2))
        t.check("numbered: the second firing finds the SAME folder (no churn through the numbers)",
                listing(vol) == sorted([".shedkeeper-store", login, login + ".2", login + ".3"])
                and len(versions_of(third, "b.v_")) == 1, listing(vol))
        t.check("numbered: the folders that are not ours are untouched",
                all(x in tree(vol) for x in before) and not os.path.exists(os.path.join(vol, login, "log.jsonl"))
                and not os.path.exists(os.path.join(vol, login + ".2", "log.jsonl")), tree(vol))
        t.check("numbered: the label of the new folder holds my SID", jload(os.path.join(third, USER_LABEL_NAME)).get("sid") == real_sid())
        # the OWNER decides, not the label: the same closed folder, its owner read as somebody else
        with patched(store, "read_owner", lambda p: FOREIGN_SID):
            ok, why = store.judge_personal(third, settings())
        t.check("numbered: a folder whose OWNER is not me is not mine, whatever its label and permissions say",
                not ok and "owner" in why, (ok, why))
        with patched(store, "read_owner", lambda p: store.ADMINS_SID):
            ok, why = store.judge_personal(third, settings())
        t.check("numbered: an owner that is the Administrators group (an elevated session) is accepted", ok, why)
        def no_owner(p):
            raise OSError(1, "no owners on this volume", p)
        with patched(store, "read_owner", no_owner):
            ok_t, why_t = store.judge_personal(third, settings())
            ok_f, _why_f = store.judge_personal(third, settings(private_store=False))
            with set_attr(store, "_volume_has_acls", lambda p: False):
                ok_x, _why_x = store.judge_personal(third, settings())
        t.check("numbered: an owner that cannot be read: refused on a volume that keeps owners (private_store on or off); "
                "the label decides only on a volume without owners",
                not ok_t and not ok_f and "owner cannot be read" in why_t and ok_x, (ok_t, why_t, ok_f, ok_x))


def t_n2_creation_report(t):
    sb = sandbox("n2r")
    login, sid = real_login(), real_sid()
    f = write(os.path.join(sb, "p", "a.txt"), "x\n")
    big = write(os.path.join(sb, "p", "big.bin"), b"\0" * (1536 * 1024), binary=True)
    vol = os.path.join(sb, "vol")
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
        obj = one_json(out)
        msg = (obj or {}).get("systemMessage", "")
        pers = os.path.join(vol, login)
        t.check("report: the first firing, which makes the folder, carries a top-level systemMessage in ONE JSON line "
                "beside the additionalContext", obj is not None and isinstance(msg, str) and msg
                and "additionalContext" in obj.get("hookSpecificOutput", {}), (rc, out[:300], err[:120]))
        t.check("report: ONE paragraph: where, closed to others, how to switch off, no number text",
                "\n" not in msg and norm(pers).lower() in msg.replace("\\", "/").lower() and "closed to other users" in msg
                and "private_store = false" in msg and "has the number" not in msg, msg)
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
        t.check("report: a later firing (the folder exists) carries no systemMessage", rc == 0 and "systemMessage" not in out, out[:200])
        rp = os.path.join(pers, "README.txt")
        raw = open(rp, "rb").read() if os.path.isfile(rp) else b""
        txt = raw.decode("utf-8-sig", "replace")
        t.check("report: README.txt is in the personal folder, UTF-8 with a BOM", raw[:3] == b"\xef\xbb\xbf", raw[:20])
        full = jload(os.path.join(pers, USER_LABEL_NAME)).get("full_name", "")
        t.check("report: README.txt names for whom (login, full name), when, closed or not, how to find and bring back, do not delete by hand",
                login in txt and (not full or full in txt) and "Created 20" in txt and "Closed to other users: yes" in txt
                and "find <part of the path>" in txt and "restore <id>" in txt and "do not delete anything here by hand" in txt, txt[:600])
        recs = lines(os.path.join(pers, "log.jsonl"))
        made = [r for r in recs if r.get("event") == "store_created"]
        t.check("report: log.jsonl has a store_created line (path, name, number, private)",
                len(made) == 1 and made[0].get("number") == 1 and made[0].get("private") is True
                and made[0].get("name") == login and norm(made[0].get("path", "")) == norm(pers), recs)
    # --- a numbered folder: the report says why
    vol = vol_with_label(sb, "vol2")
    write(os.path.join(vol, login, USER_LABEL_NAME), json.dumps({"product": "shedkeeper", "sid": FOREIGN_SID, "login": login,
                                                                  "full_name": "", "created": "x"}))
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
        msg = (one_json(out) or {}).get("systemMessage", "")
        t.check("report: a numbered name says why: the name is taken by a folder that is not this user's store",
                ("has the number 2 because the name %s is taken by a folder that is not this user's store" % login) in msg
                and "another SID" in msg and (login + ".2") in msg, msg)
        txt = open(os.path.join(vol, login + ".2", "README.txt"), "rb").read().decode("utf-8-sig", "replace")
        t.check("report: README.txt of the numbered folder says why the name has a number", "Why the name has a number" in txt, txt[:500])
        recs = lines(os.path.join(vol, login + ".2", "log.jsonl"))
        t.check("report: log.jsonl says number 2", any(r.get("event") == "store_created" and r.get("number") == 2 for r in recs), recs)
    # --- private_store = false: the report says it is NOT closed
    ini_f = write(os.path.join(sb, "f.ini"), "private_store = false\n")
    with env(SHEDKEEPER_STORE_ROOT=os.path.join(sb, "vol3"), SHEDKEEPER_INI=ini_f):
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
        msg = (one_json(out) or {}).get("systemMessage", "")
        t.check("report: private_store = false -> the message says it is NOT closed to other users",
                "NOT closed to other users" in msg and "private_store = false" in msg, msg)
        txt = open(os.path.join(sb, "vol3", login, "README.txt"), "rb").read().decode("utf-8-sig", "replace")
        t.check("report: private_store = false -> README.txt says NO", "Closed to other users: NO" in txt, txt[:400])
    # --- a REFUSAL (exit 0 + deny) carries the systemMessage too
    ini_d = write(os.path.join(sb, "d.ini"), "max_copy_mb = 1\non_too_large = deny\n")
    with env(SHEDKEEPER_STORE_ROOT=os.path.join(sb, "vol4"), SHEDKEEPER_INI=ini_d):
        rc, out, err = hook_sub(PRE_TOOL, edit_event(big))
        obj = one_json(out)
        t.check("report: a refusal that follows the creation (no copy of a big file) still carries the systemMessage",
                obj is not None and obj.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
                and "personal store" in obj.get("systemMessage", ""), (rc, out[:300], err[:120]))
    # --- the session hook: SessionStart = guide + message, SessionEnd = message only
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    tp = write(os.path.join(sb, "cc", "proj-folder", "s1.jsonl"), "1\n")
    for ev_name, vname in (("SessionStart", "vol5"), ("SessionEnd", "vol6")):
        with env(SHEDKEEPER_STORE_ROOT=os.path.join(sb, vname), SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
            ev = {"hook_event_name": ev_name, "transcript_path": tp, "session_id": "s1", "cwd": proj}
            rc, out, err = hook_sub(SESSION, ev)
            obj = one_json(out)
            has_ctx = "additionalContext" in (obj or {}).get("hookSpecificOutput", {})
            t.check("report: the session hook %s makes the folder and says so in a systemMessage%s" % (
                        ev_name, " beside the guide" if ev_name == "SessionStart" else " (no context at SessionEnd)"),
                    obj is not None and "personal store" in obj.get("systemMessage", "") and has_ctx == (ev_name == "SessionStart"),
                    (rc, out[:300], err[:120]))
            rc, out, err = hook_sub(SESSION, ev)
            t.check("report: the session hook %s is quiet about it the second time" % ev_name,
                    "systemMessage" not in out, out[:200])
    # --- the README could not be written: the store is made, the person is told
    with env(SHEDKEEPER_STORE_ROOT=os.path.join(sb, "vol7"), SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        st = settings()
        root = store.store_root("x", st)
        with patched(store, "_write_readme", lambda r, text: (False, "OSError: disk says no")):
            ok, note, rep = store.ensure_store_report(root, st)
        t.check("report: README.txt that could not be written does not stop the store, and the message says so",
                ok and rep and not rep["readme"] and "could NOT be written" in store.creation_report(rep)
                and "disk says no" in store.creation_report(rep), (ok, note, rep))
        t.check("report: creation_report(None) is empty", store.creation_report(None) == "")


def t_n2_docs(t):
    readme = read(os.path.join(KIT, "README.md"))
    low = readme.lower()
    p3 = readme[readme.index("3. **"):readme.index("Plain Python")]
    t.check("README promise 3: by the person's word -- the agent finds, explains, brings back after consent; or one command",
            "by your word" in p3 and "once you say yes" in p3 and "yourself" in p3 and "only you do" in p3 and "human's hand" not in readme, p3)
    ag = readme[readme.index("## For your agent"):readme.index("## Related tools")]
    t.check("README, for your agent: two scenarios (a) a loss noticed, (b) the person asks, and never restore without the word",
            "(a) **you noticed a loss**" in ag and "(b) **the person asks**" in ag and "only after the person agrees" in ag
            and "Never restore on your own initiative" in ag and "all matching versions" in ag, ag)
    hist = readme[readme.index("## History"):readme.index("## License")]
    t.check("README history: one sentence that the protection grew after an agent deleted files, no tool names",
            "an agent deleted a person's files" in hist and "Desktop Commander" not in readme, hist)
    t.check("README: ownership and permissions recognise the folder, a taken name -> the next number, the report, README.txt",
            "owner and permissions" in readme and "<login name>.2" in readme and "README.txt" in readme and "tells you so" in readme, "missing")
    t.check("README: nothing in the text calls a taken name a refusal any more", "refuses to use it" not in readme)


# =============================================================================================
# 10. the personal store: what two code reviews found: each test here is red on 0.2.0
# =============================================================================================

def spawn_spy():
    """(calls, fake): `fake` stands in for subprocess.run / Popen, records the command and refuses to start it."""
    calls = []

    def fake(*a, **k):
        calls.append(a[0] if a else k)
        raise OSError("blocked by the test: no program may be started here")
    return calls, fake


def icacls_grant(path, *specs):
    return subprocess.run([os.path.join(WINDIR, "System32", "icacls.exe"), path, "/grant:r"] + list(specs),
                          capture_output=True, timeout=60).returncode


def junction(link, target):
    """A junction `link` -> `target`; None when mklink /J is not available here."""
    try:
        p = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True, text=True, timeout=20)
        return os.path.exists(link) if p.returncode == 0 else None
    except Exception:
        return None


def load_module(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cli_inproc(*args):
    """shedkeeper.py main() in THIS process (so the store can be patched): (exit code, stdout, stderr)."""
    cli = load_module("sk_cli_n3", SHEDKEEPER_PY)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(args))
    return rc, out.getvalue(), err.getvalue()


def t_n3_created_closed(t):
    """The personal folder is CREATED closed (the descriptor goes to CreateDirectoryW), not made open
    and closed afterwards; the new folder is required to be empty; the folder under its own name is judged again."""
    sb = sandbox("n3c")
    login, sid = real_login(), real_sid()
    vol = os.path.join(sb, "vol")
    pers = os.path.join(vol, login)
    made = []
    real_mkdir = os.mkdir

    def spy_mkdir(p, *a, **k):
        made.append(os.path.abspath(p))
        return real_mkdir(p, *a, **k)

    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        with patched(os, "mkdir", spy_mkdir):
            ok, note = store.ensure_store(pers, settings())
        t.check("closed at birth: the personal folder is made, closed (DACL read back)",
                ok and os.path.isdir(pers) and store.check_private(pers, sid)[0], (ok, note))
        named = [m for m in made if os.path.basename(m).startswith("." + login) or m in (os.path.abspath(pers), os.path.abspath(vol))]
        t.check("closed at birth: neither the temporary folder, nor the personal one, nor the shared one was made by "
                "os.mkdir (an open folder first, closed afterwards) -- all through CreateDirectoryW with the descriptor",
                not named, made)
    # the creator, called directly: the rights are there at the first moment and the call refuses an existing name
    d = os.path.join(sb, "direct")
    mk = getattr(store, "make_dir_with_sddl", None)
    sddl = store.private_sddl(sid) if hasattr(store, "private_sddl") else None
    if mk and sddl:
        mk(d, sddl)
        prot, aces = store.read_dacl(d)
        t.check("closed at birth: right after the creator returns, the DACL is protected with exactly the user and SYSTEM",
                prot and sorted(a[3] for a in aces) == sorted([sid, "S-1-5-18"]), (prot, aces))
        try:
            mk(d, sddl)
            exists = False
        except FileExistsError:
            exists = True
        t.check("closed at birth: the creator refuses a folder that exists (FileExistsError)", exists)
    else:
        t.check("closed at birth: the creator make_dir_with_sddl exists", False, "no make_dir_with_sddl / private_sddl")
    # something planted in the new folder before the label goes in: no store, the folder is left and NAMED
    real_make = getattr(store, "make_dir_with_sddl", None)

    def planting(path, sddl_):
        real_make(path, sddl_)
        if os.path.basename(path).startswith("."):
            os.mkdir(os.path.join(path, "trash"))
    vol2 = os.path.join(sb, "vol2")
    pers2 = os.path.join(vol2, login)
    with env(SHEDKEEPER_STORE_ROOT=vol2, SHEDKEEPER_INI=os.path.join(sb, "no.ini")), set_attr(store, "make_dir_with_sddl", planting):
        ok, why = store.ensure_store(pers2, settings())
    left = [n for n in listing(vol2) if ".part-" in n]
    t.check("planted entry: the new folder was not empty -> no store, the entry is named, the folder is left (never emptied)",
            not ok and "not empty" in why and "trash" in why and len(left) == 1 and left[0] in why and not os.path.exists(pers2)
            and os.path.isdir(os.path.join(vol2, left[0], "trash")), (ok, why, left))
    # something appears after the label and the README: the folder under its own name is judged again
    vol3 = os.path.join(sb, "vol3")
    pers3 = os.path.join(vol3, login)

    def readme_and_foreign(root, text):
        write(os.path.join(root, "foreign.txt"), "x")
        return True, ""
    with env(SHEDKEEPER_STORE_ROOT=vol3, SHEDKEEPER_INI=os.path.join(sb, "no.ini")), patched(store, "_write_readme", readme_and_foreign):
        ok, why = store.ensure_store(pers3, settings())
    rej = [n for n in listing(vol3) if ".rejected-" in n]
    t.check("after the rename: a folder holding something that is not ours is NOT accepted -- renamed out of the way "
            "(not deleted) and named", not ok and "foreign.txt" in why and "NOT accepted" in why and not os.path.exists(pers3)
            and len(rej) == 1 and os.path.isfile(os.path.join(vol3, rej[0], "foreign.txt")), (ok, why, listing(vol3)))
    # a reparse point among the files, named like our own temporary file (so the name test alone would let it pass)
    target = os.path.join(sb, "elsewhere")
    os.makedirs(target)
    if not junction(os.path.join(sb, "probe_link"), target):
        t.skip("after the rename: a junction inside is NOT accepted", "mklink /J not available here")
        return
    vol4 = os.path.join(sb, "vol4")
    pers4 = os.path.join(vol4, login)

    def readme_and_link(root, text):
        junction(os.path.join(root, "README.txt.part-1"), target)
        return True, ""
    with env(SHEDKEEPER_STORE_ROOT=vol4, SHEDKEEPER_INI=os.path.join(sb, "no.ini")), patched(store, "_write_readme", readme_and_link):
        ok, why = store.ensure_store(pers4, settings())
    t.check("after the rename: a junction inside (named like our temporary file) is NOT accepted",
            not ok and "reparse point" in why and not os.path.exists(pers4), (ok, why))


def t_n3_parallel_start(t):
    """A race found by the probe of this act (6 hooks at once on an empty volume): the folder that wins the rename is
    used by the other hooks at once, so the check after the rename must let their entries (log.jsonl, versions ...)
    stand -- and must still refuse a junction under such a name. A guard, not a test red on 0.2.0 (that kit never
    judged the folder after the rename)."""
    sb = sandbox("n3p")
    login = real_login()
    f = write(os.path.join(sb, "p", "a.txt"), "x\n")
    vol = os.path.join(sb, "vol")
    pers = os.path.join(vol, login)

    def readme_and_concurrent(root, text):          # what another hook of mine does a moment after the rename
        os.makedirs(os.path.join(root, "versions", "2026-10-01"))
        write(os.path.join(root, "log.jsonl"), "{}\n")
        return True, ""
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")), patched(store, "_write_readme", readme_and_concurrent):
        ok, why = store.ensure_store(pers, settings())
    t.check("race: entries the other hooks of this user put into the folder (versions, log.jsonl) do not make it 'not ours'",
            ok and os.path.isdir(pers) and not [n for n in listing(vol) if ".rejected-" in n], (ok, why, listing(vol)))
    target = os.path.join(sb, "elsewhere")
    os.makedirs(target)
    if junction(os.path.join(sb, "probe_link"), target):
        vol2 = os.path.join(sb, "vol2")

        def readme_and_versions_link(root, text):
            junction(os.path.join(root, "versions"), target)
            return True, ""
        with env(SHEDKEEPER_STORE_ROOT=vol2, SHEDKEEPER_INI=os.path.join(sb, "no.ini")), patched(store, "_write_readme", readme_and_versions_link):
            ok2, why2 = store.ensure_store(os.path.join(vol2, login), settings())
        t.check("race: a JUNCTION named `versions` in the new folder is refused (the attack the check exists for)",
                not ok2 and "reparse point" in why2 and not os.path.exists(os.path.join(vol2, login)), (ok2, why2))
    else:
        t.skip("race: a junction named `versions` is refused", "mklink /J not available here")
    # six hooks at once on an empty volume: nobody is refused, nothing is renamed away, one folder results
    vol3 = os.path.join(sb, "vol3")
    ev = json.dumps(edit_event(f)).encode("utf-8")
    e = clean_env()
    e.update({"SHEDKEEPER_STORE_ROOT": vol3, "SHEDKEEPER_INI": os.path.join(sb, "no.ini")})
    procs = [subprocess.Popen([PY, PRE_TOOL], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e)
             for _ in range(6)]
    for p in procs:
        p.stdin.write(ev)
        p.stdin.close()
    outs = [(p.stdout.read().decode("utf-8", "replace"), p.wait()) for p in procs]
    t.check("race: six hooks at once on an empty volume -- exit 0 for all, none refused",
            all(rc == 0 and verdict(o) != "deny" for o, rc in outs), [o[:200] for o, _rc in outs if verdict(o) == "deny"])
    t.check("race: ... one personal folder, nothing renamed away, no label leftover",
            os.path.isdir(os.path.join(vol3, login)) and not [n for n in listing(vol3) if ".rejected-" in n or n.startswith(".shedkeeper-store.part")],
            listing(vol3))


def t_n3_sid_from_token(t):
    """The SID comes from the process token; no program is started on the hook path."""
    sb = sandbox("n3s")
    login, sid = real_login(), real_sid()
    f = write(os.path.join(sb, "p", "a.txt"), "x\n")
    vol = os.path.join(sb, "vol")
    forget_identity()
    calls, fake = spawn_spy()
    try:
        with patched(subprocess, "run", fake), patched(subprocess, "Popen", fake):
            try:
                got = store._lookup_sid()
            except Exception as e:
                got = ("exception", "%s: %s" % (e.__class__.__name__, e))
    finally:
        forget_identity()
    t.check("SID: _lookup_sid gives the SID of this account (the same one whoami reports) and starts no program",
            got == (sid, "") and not calls, (got, calls))
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        st = settings()
        store.ensure_store(store.store_root("x", st), st)             # the store exists
        for label, ev in (("an Edit of an existing file", edit_event(f)), ("a Bash `ls`", bash_event("ls", sb)),
                          ("a Bash that overwrites a file", bash_event("echo y > %s" % norm(f), sb))):
            forget_identity()
            calls, fake = spawn_spy()
            try:
                with patched(subprocess, "run", fake), patched(subprocess, "Popen", fake):
                    rc, out = hook_inproc(ev)
            finally:
                forget_identity()
            t.check("SID: %s -- the whole hook run starts no program, and is not refused" % label,
                    not calls and verdict(out) != "deny", (calls, out[:200]))
    t.check("SID: the helpers that started whoami/icacls are gone from the module",
            not hasattr(store, "_icacls") and not hasattr(store, "_system32"), "still there")


def t_n3_lazy_root_and_name(t):
    """The root of the store is computed only when there is something to copy; the full name of the account
    only when a label is written."""
    sb = sandbox("n3l")
    f = write(os.path.join(sb, "p", "a.txt"), "x\n")
    vol = os.path.join(sb, "vol")
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        st = settings()
        names = []
        real_full = store._full_name
        real_root = store.store_root
        roots = []

        def spy_full(login):
            names.append(login)
            return real_full(login)

        def spy_root(path, s):
            roots.append(path)
            return real_root(path, s)

        forget_identity()
        with patched(store, "_full_name", spy_full):
            store.ensure_store(real_root("x", st), st)
        t.check("lazy: the full name is looked up when the label is written", len(names) >= 1, names)
        forget_identity()
        del names[:]
        with patched(store, "_full_name", spy_full), patched(store, "store_root", spy_root):
            rc, out = hook_inproc(edit_event(f))
        t.check("lazy: an Edit with the store in place does not look the full name up", not names and verdict(out) != "deny", (names, out[:200]))
        for label, ev, want in (("a harmless Bash `ls`", bash_event("ls", sb), 0), ("a Bash `git status`", bash_event("git status", sb), 0),
                                ("an Edit of a file that does not exist", edit_event(os.path.join(sb, "p", "new.txt")), 0),
                                ("a Bash that overwrites an existing file", bash_event("echo y > %s" % norm(f), sb), 1)):
            del roots[:]
            forget_identity()
            with patched(store, "store_root", spy_root):
                rc, out = hook_inproc(ev)
            t.check("lazy: %s -- the root of the store is %s" % (label, "computed (there is a file to copy)" if want else "NOT computed"),
                    (len(roots) >= 1) == bool(want), (roots, out[:160]))


def t_n3_volume_rights(t):
    """The shared volume folder is made closed: others may list it, make their OWN folder and read the label,
    nothing more. Read from the DACL and from icacls; a second real user is not available (said in the report)."""
    sb = sandbox("n3v")
    login, sid = real_login(), real_sid()
    vol = os.path.join(sb, "vol")
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        st = settings()
        ok, note = store.ensure_store(store.store_root("x", st), st)
    prot, aces = store.read_dacl(vol)
    AU, ALL = "S-1-5-11", {"S-1-1-0", "S-1-5-32-545"}
    au_dir = [a for a in aces if a[3] == AU and not a[1] & 0x08]
    au_files = [a for a in aces if a[3] == AU and a[1] & 0x08]
    wr = 0x2 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000
    t.check("volume folder: made, protected, nobody of Everyone / Users in it",
            ok and prot and not [a for a in aces if a[3] in ALL], (ok, note, prot, aces))
    t.check("volume folder: other authenticated users may list, walk through and ADD A SUBFOLDER on the folder itself (their own folder)",
            len(au_dir) == 1 and (au_dir[0][2] & 0x4) and (au_dir[0][2] & 0x1) and (au_dir[0][2] & 0x20), au_dir)
    t.check("volume folder: and nothing else there -- no adding files, deleting, renaming, changing rights, no ownership",
            len(au_dir) == 1 and not (au_dir[0][2] & wr), au_dir)
    t.check("volume folder: on the files inside (the label) they may only READ",
            len(au_files) == 1 and not (au_files[0][2] & (wr | 0x4 | 0x40000000 | 0x10000000)), au_files)
    lab = store.read_dacl(os.path.join(vol, ".shedkeeper-store"))[1]
    lab_au = [a for a in lab if a[3] == AU]
    t.check("volume folder: the label file, as it came out by inheritance, cannot be written, renamed or deleted by them",
            len(lab_au) == 1 and not (lab_au[0][2] & wr) and (lab_au[0][2] & 0x1), lab)
    cv = getattr(store, "check_volume_rights", None)
    t.check("volume folder: check_volume_rights reads it back as closed", cv is not None and cv(vol)[0], cv(vol) if cv else "no check_volume_rights")
    txt = icacls_text(vol)
    t.check("volume folder: icacls (an independent reader) shows (RX,AD) and (OI)(IO)(GR) for them, and not Modify / Write for anybody but the owners",
            "(RX,AD)" in txt and "(OI)(IO)(GR)" in txt and "(M)" not in txt and "(W)" not in txt, txt)
    t.check("volume folder: icacls on the label: read only for them, inherited",
            "(I)(R)" in icacls_text(os.path.join(vol, ".shedkeeper-store")), icacls_text(os.path.join(vol, ".shedkeeper-store")))
    # controls: a folder that inherits is not closed; one that gives Modify to authenticated users is not closed
    plain = os.path.join(sb, "plain")
    os.makedirs(plain)
    t.check("volume folder control: a folder that inherits its permissions is NOT judged closed",
            cv is not None and not cv(plain)[0], cv(plain) if cv else "no check_volume_rights")
    loose = os.path.join(sb, "loose")
    if getattr(store, "make_dir_with_sddl", None):
        store.make_dir_with_sddl(loose, "D:P(A;OICI;FA;;;%s)(A;OICI;0x1301bf;;;AU)" % sid)
        t.check("volume folder control: authenticated users with Modify -> NOT judged closed",
                cv is not None and not cv(loose)[0] and "write" in cv(loose)[1], cv(loose) if cv else "no check_volume_rights")
    # a shared folder made by an earlier version (open) is taken as it is: its rights are not touched, status says so
    old_vol = os.path.join(sb, "oldvol")
    write(os.path.join(old_vol, ".shedkeeper-store"), json.dumps({"product": "shedkeeper", "created": "x"}))
    before = store.read_dacl(old_vol)
    with env(SHEDKEEPER_STORE_ROOT=old_vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        ok, note = store.ensure_store(store.store_root("x", st), st)
        rc, out = shedkeeper_cli("status")
    t.check("volume folder: an existing open shared folder is used as it is (rights untouched) and `status` says NOT closed as a shared folder",
            ok and store.read_dacl(old_vol) == before and "NOT closed as a shared folder" in out, (ok, note, out[-500:]))
    # the personal folder of private_store = false inherits from the volume folder and is writable by its owner
    ini_f = write(os.path.join(sb, "f.ini"), "private_store = false\n")
    vol_f = os.path.join(sb, "volf")
    f = write(os.path.join(sb, "p", "a.txt"), "x\n")
    with env(SHEDKEEPER_STORE_ROOT=vol_f, SHEDKEEPER_INI=ini_f):
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
    pf = os.path.join(vol_f, login)
    prot_f, aces_f = store.read_dacl(pf) if os.path.isdir(pf) else (None, [])
    t.check("private_store = false in a closed shared folder: the folder inherits (not protected), the owner has full control, "
            "and the copy was taken",
            os.path.isdir(pf) and prot_f is False and any(a[3] == sid and (a[2] & 0x1f01ff) == 0x1f01ff for a in aces_f)
            and len(versions_of(pf, "a.v_")) == 1, (rc, out[:160], err[:160], aces_f))


def t_n3_extra_rights_stay_mine(t):
    """A legitimate third entry in the permissions of my folder (an administrator, a backup
    program) does not make it 'not mine': the hook keeps using it, find sees its copies, nothing is re-closed,
    no <login>.2 is made -- and status, install check and the session start say `private: NO` with the list."""
    sb = sandbox("n3e")
    login, sid = real_login(), real_sid()
    f = write(os.path.join(sb, "p", "a.txt"), "v1\n")
    vol = os.path.join(sb, "vol")
    pers = os.path.join(vol, login)
    home = os.path.join(sb, "home")
    os.makedirs(home)
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    tp = write(os.path.join(sb, "cc", "proj-folder", "s1.jsonl"), "1\n")
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini"), SHEDKEEPER_USER_HOME=home):
        hook_sub(PRE_TOOL, edit_event(f))
        t.check("extra rights: the closed folder is made first", os.path.isdir(pers) and store.check_private(pers, sid)[0], listing(vol))
        rc_g = icacls_grant(pers, "*S-1-1-0:(OI)(CI)R")
        dacl = store.read_dacl(pers)
        t.check("extra rights: a third entry (Everyone, read) is in place -- an administrator's or a backup program's doing",
                rc_g == 0 and len(dacl[1]) == 3, (rc_g, dacl))
        write(f, "v2\n")
        rc, out, err = hook_sub(PRE_TOOL, edit_event(f))
        t.check("extra rights: the hook keeps using the SAME folder: the second copy is in it, no <login>.2",
                verdict(out) != "deny" and len(versions_of(pers, "a.v_")) == 2 and not os.path.exists(pers + ".2"),
                (out[:200], err[:120], listing(vol)))
        t.check("extra rights: the tool did not change the permissions (it does not re-close on its own)", store.read_dacl(pers) == dacl, store.read_dacl(pers))
        ok, why = store.judge_personal(pers, settings())
        t.check("extra rights: judge_personal still says it is mine", ok, why)
        rc, out = shedkeeper_cli("find", "a.txt")
        ids = re.findall(r"v:\d{4}-\d{2}-\d{2}:\d+", out)
        t.check("extra rights: find lists BOTH copies (two version ids), both inside my folder, exit 0",
                rc == 0 and len(set(ids)) == 2 and out.lower().replace("\\", "/").count(norm(pers).lower() + "/versions/") >= 2, (rc, out))
        rc, out = shedkeeper_cli("status")
        t.check("extra rights: status says `private: NO` and lists the entries (S-1-1-0)",
                "-- private: NO" in out and "S-1-1-0" in out and "skipped" not in out, out)
        inst("install", "--project", proj)
        rc, out = inst("check", "--project", proj)
        t.check("extra rights: install check FAILS (exit 1) and says private: NO -- the branch is reachable now",
                rc == 1 and "private: NO" in out, (rc, out[-600:]))
        ev = {"hook_event_name": "SessionStart", "transcript_path": tp, "session_id": "s1", "cwd": proj}
        rc, out, err = hook_sub(SESSION, ev)
        ctx = (one_json(out) or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        t.check("extra rights: the session start puts a WARNING into the context: the store is NOT closed, with the list",
                "WARNING" in ctx and "NOT closed to other users" in ctx and "S-1-1-0" in ctx, (rc, out[:400], err[:120]))
        t.check("extra rights: ... and it is still the guide (the store is usable)", "NOT usable" not in ctx, ctx[:300])


def t_n3_find_not_searched(t):
    """`find` and `restore` never say 'nothing' about a place they did not look at."""
    sb = sandbox("n3f")
    login, sid = real_login(), real_sid()
    f = write(os.path.join(sb, "p", "a.txt"), "v1\n")
    g = write(os.path.join(sb, "p", "b.txt"), "to the trash\n")
    vol = os.path.join(sb, "vol")
    pers = os.path.join(vol, login)
    old_store = os.path.join(vol, "old_store")
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        hook_sub(PRE_TOOL, edit_event(f))
        os.rename(pers, old_store)                          # my own store under a name the tool does not examine
        rc, out = shedkeeper_cli("find", "a.txt")
        t.check("not searched: my store under another name -> `find` says NOT SEARCHED, names the folder and why, exits 2, "
                "and does not say plain 'nothing found'",
                rc == 2 and "NOT SEARCHED" in out and "old_store" in out and "its name is not" in out and "NOT everything" in out, (rc, out))
        rc, out = shedkeeper_cli("trash", g, "--reason", "n3")
        rc, out = shedkeeper_cli("find", "b.txt")
        mid = re.search(r"t:\d{4}-\d{2}-\d{2}:\d+", out)
        t.check("not searched: a hit is printed, and the unsearched folder is still named (exit 2)",
                mid and rc == 2 and "NOT SEARCHED" in out and "old_store" in out, (rc, out))
        rc, out = shedkeeper_cli("restore", "t:2020-01-01:1")
        t.check("not searched: restore of an id that is not found says it was NOT searched everywhere, exit 2 (not a plain refusal)",
                rc == 2 and "NOT SEARCHED" in out and "old_store" in out, (rc, out))
        rc, out = shedkeeper_cli("restore", mid.group(0) if mid else "t:x")
        t.check("not searched: a restore that is done says in the same output what was not searched, exit 0",
                rc == 0 and read(g) == "to the trash\n" and "NOT SEARCHED" in out, (rc, out))
        rc, out = shedkeeper_cli("status")
        t.check("not searched: status names the folder too", "NOT SEARCHED" in out and "old_store" in out, out)
    # the owner of a folder with my label is somebody else: named, with the reason
    vol2 = os.path.join(sb, "vol2")
    with env(SHEDKEEPER_STORE_ROOT=vol2, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        hook_sub(PRE_TOOL, edit_event(f))
        with patched(store, "read_owner", lambda p: FOREIGN_SID):
            lines_ = store.unsearched_lines(settings())
        t.check("not searched: a folder with my label whose OWNER is somebody else is named with the reason",
                len(lines_) == 1 and "owner" in lines_[0] and os.path.join(vol2, login).lower() in lines_[0].lower(), lines_)
    # my SID cannot be found: NOTHING was searched, and it is said
    vol3 = os.path.join(sb, "vol3")
    with env(SHEDKEEPER_STORE_ROOT=vol3, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        hook_sub(PRE_TOOL, edit_event(f))
        forget_identity()
        try:
            with set_attr(store, "_lookup_sid", lambda: (None, "the token is gone")):
                forget_identity()
                hits, problems = store.find("a.txt", settings())
                rc, out, err = cli_inproc("find", "a.txt")
                st_, msg = store.restore("t:2026-01-01:1", settings())
                rc2, out2, err2 = cli_inproc("restore", "t:2026-01-01:1")
        finally:
            forget_identity()
        t.check("not searched: no SID -> find reports that NOTHING was searched, with the reason, exit 2",
                not hits and any("NOT SEARCHED" in p and "nothing was searched" in p and "token is gone" in p for p in problems)
                and rc == 2 and "NOT SEARCHED" in err and "NOT everything" in out, (hits, problems, rc, out, err))
        t.check("not searched: no SID -> restore is 'blind' (exit 2), not a plain refusal",
                st_ == "blind" and rc2 == 2 and "nothing was searched" in out2, (st_, msg, rc2, out2))


def t_n3_owner_unreadable(t):
    """An owner that cannot be read is believed on the label only on a volume without owners; a link is not a store."""
    sb = sandbox("n3o")
    login = real_login()
    f = write(os.path.join(sb, "p", "a.txt"), "x\n")
    vol = os.path.join(sb, "vol")
    pers = os.path.join(vol, login)
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        hook_sub(PRE_TOOL, edit_event(f))

        def no_owner(p):
            raise OSError(5, "Access is denied", p)
        with patched(store, "read_owner", no_owner):
            ok1, why1 = store.judge_personal(pers, settings())
            ok2, why2 = store.judge_personal(pers, settings(private_store=False))
        t.check("owner unreadable, a volume that KEEPS owners: not mine -- with private_store on and off alike",
                not ok1 and not ok2 and "keeps owners" in why2, (ok1, why1, ok2, why2))
        with patched(store, "read_owner", no_owner), set_attr(store, "_volume_has_acls", lambda p: False):
            ok3, why3 = store.judge_personal(pers, settings())
        t.check("owner unreadable, a volume WITHOUT owners (exFAT): the label decides", ok3, why3)
        has = getattr(store, "_volume_has_acls", None)
        t.check("the volume of the test folder (NTFS) reports persistent permissions through the real call",
                has is not None and has(pers) is True, has(pers) if has else "no _volume_has_acls")
        # a junction with the name of my folder is not a store, however good the label behind it
        vol2 = os.path.join(sb, "vol2")
        os.makedirs(vol2)
        link = os.path.join(vol2, login)
        made = junction(link, pers)
        if made:
            ok4, why4 = store.judge_personal(link, settings())
            t.check("a junction named like my folder, pointing at a real store of mine, is NOT a store", not ok4 and "link" in why4, (ok4, why4))
        else:
            t.skip("a junction named like my folder is NOT a store", "mklink /J not available")


def t_n3_session_reasons(t):
    """The session start names WHY the store cannot be used and does not promise copies; a failing
    report is logged, in both hooks."""
    sb = sandbox("n3r")
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    tp = write(os.path.join(sb, "cc", "proj-folder", "s1.jsonl"), "1\n")
    f = write(os.path.join(sb, "p", "a.txt"), "x\n")
    filep = write(os.path.join(sb, "a_file"), "i am a file\n")
    ev = {"hook_event_name": "SessionStart", "transcript_path": tp, "session_id": "s1", "cwd": proj}
    # the folder does not exist yet and cannot be made (a parent is a file): check_available says yes, the creation says no
    with env(SHEDKEEPER_STORE_ROOT=os.path.join(filep, "sub", "vol"), SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        rc, out, err = hook_sub(SESSION, ev)
        obj = one_json(out)
        ctx = (obj or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        t.check("session reasons: the creation failed -> a WARNING with the REASON in the context, one JSON line, exit 0",
                rc == 0 and obj is not None and "WARNING" in ctx and "not usable" in ctx and "could not create the store" in ctx, (rc, out[:500], err[:120]))
        t.check("session reasons: ... and the guide does NOT promise copies (no 'a copy is saved before an edit')",
                "a copy is saved before an edit" not in ctx and "NOT usable" in ctx, ctx[:500])
    # the other side: a usable store keeps the old guide
    with env(SHEDKEEPER_STORE_ROOT=os.path.join(sb, "vol"), SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        rc, out, err = hook_sub(SESSION, ev)
        ctx = (one_json(out) or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        t.check("session reasons: a usable store -> the guide as before", "a copy is saved before an edit" in ctx and "NOT usable" not in ctx, ctx[:300])
    # a report that fails: logged, and the decision / the guide stand
    vol = os.path.join(sb, "volr")

    def boom(rep):
        raise KeyError("report exploded")
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")), patched(store, "creation_report", boom):
        rc, out = hook_inproc(edit_event(f))
    pers = os.path.join(vol, real_login())
    recs = lines(os.path.join(pers, "log.jsonl")) if os.path.isfile(os.path.join(pers, "log.jsonl")) else []
    t.check("report failure (pre_tool): the Edit is still allowed and the failure is in log.jsonl as report_error",
            verdict(out) != "deny" and any(r.get("event") == "report_error" and "report exploded" in r.get("error", "") for r in recs), (out[:200], recs))
    vol2 = os.path.join(sb, "volr2")
    sess = load_module("sk_session_n3", SESSION)
    old_in = sys.stdin
    buf = io.StringIO()
    sys.stdin = _Stdin(json.dumps(ev).encode("utf-8"))
    try:
        with env(SHEDKEEPER_STORE_ROOT=vol2, SHEDKEEPER_INI=os.path.join(sb, "no.ini")), patched(store, "creation_report", boom), \
                contextlib.redirect_stdout(buf):
            rc = sess.main()
    finally:
        sys.stdin = old_in
    pers2 = os.path.join(vol2, real_login())
    recs2 = lines(os.path.join(pers2, "log.jsonl")) if os.path.isfile(os.path.join(pers2, "log.jsonl")) else []
    ctx2 = (one_json(buf.getvalue()) or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
    t.check("report failure (session): the guide is still given, and the failure is in log.jsonl as report_error",
            rc == 0 and "shedkeeper: " in ctx2 and any(r.get("event") == "report_error" for r in recs2), (rc, buf.getvalue()[:300], recs2))


def t_n3_system_account_and_leftovers(t):
    """The account SYSTEM: one entry is the whole list (the old comparison wanted two). The leftovers of a creation
    are named by status."""
    sb = sandbox("n3y")
    login, sid = real_login(), real_sid()
    d = os.path.join(sb, "as_system")
    os.makedirs(d)
    subprocess.run([os.path.join(WINDIR, "System32", "icacls.exe"), d, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F"],
                   capture_output=True, timeout=60)
    prot, aces = store.read_dacl(d)
    ok, why = store.check_private(d, "S-1-5-18")
    icacls_grant(d, "*%s:(OI)(CI)F" % sid)                  # give my own access back: the test folder stays usable
    t.check("system account: protected, ONE entry (SYSTEM) -> check_private accepts it for the user S-1-5-18",
            prot and len(aces) == 1 and ok, (prot, aces, ok, why))
    mk = getattr(store, "make_dir_with_sddl", None)
    if mk and hasattr(store, "private_sddl"):
        d2 = os.path.join(sb, "made_as_system")
        mk(d2, store.private_sddl("S-1-5-18"))
        prot2, aces2 = store.read_dacl(d2)
        t.check("system account: the descriptor for the user SYSTEM holds ONE entry, not two identical ones", prot2 and len(aces2) == 1, aces2)
    # leftovers
    vol = os.path.join(sb, "vol")
    f = write(os.path.join(sb, "p", "a.txt"), "x\n")
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        hook_sub(PRE_TOOL, edit_event(f))
        write(os.path.join(vol, ".%s.part-111-222" % login, ".shedkeeper-user"), "{}")
        os.makedirs(os.path.join(vol, ".%s.rejected-1-2" % login))
        rc, out = shedkeeper_cli("status")
        t.check("leftovers: status names the temporary folder of a lost race and the rejected folder, and says the tool never deletes them",
                ".%s.part-111-222" % login in out and ".%s.rejected-1-2" % login in out and "never deletes" in out, out)
        rc, out = shedkeeper_cli("find", "a.txt")
        t.check("leftovers: they are not 'NOT SEARCHED' noise (they hold no copies)", rc == 0 and "NOT SEARCHED" not in out, (rc, out))


def t_n3_docs(t):
    readme = read(os.path.join(KIT, "README.md"))
    t.check("README: the owner and the label recognise a folder, the permissions are reported (private: NO), no re-closing, no new number",
            "do not decide whose the folder is" in readme and "does not close it again on its own" in readme, "missing")
    t.check("README: created closed from the first moment; the shared folder is closed the way $Recycle.Bin is; the SID from the process token",
            "CLOSED FROM THE FIRST MOMENT" in readme and "make their OWN folder" in readme and "token of the" in readme, "missing")
    t.check("README: the limits are named -- an older shared folder, the 99 names, the leftover folders, no second user tried",
            "NOT closed as a shared folder" in readme and "all 99 are taken" in readme and ".part-..." in readme
            and "Not tried: a" in readme, "missing")
    t.check("README: the old claim that the folder is recognised 'by its owner alone' is gone", "by its owner alone" not in readme)
    ag = readme[readme.index("## For your agent"):readme.index("## Related tools")]
    t.check("README, for your agent: `NOT SEARCHED` in the output of find / restore (exit 2) is not 'the file is gone'",
            "NOT SEARCHED" in ag and "exit code 2" in ag.replace("`", ""), ag[-700:])
    t.check("README: no whoami and no icacls as a method of the tool (icacls only as the independent reader of the rights)",
            "whoami" not in readme and readme.count("icacls") == 1, readme.count("icacls"))
    doc = read(SHEDKEEPER_PY)
    t.check("shedkeeper.py: the exit codes of find / restore tell about NOT SEARCHED", "NOT SEARCHED" in doc and "restore: 2" in doc, "missing")



# =============================================================================================
# 11. removal and re-install: removing, moving and re-installing (each test here is red on the 0.2.0 kit, whose command was
#     `python "<path>"` and whose installer knew only the entries of its own folder)
# =============================================================================================

README = os.path.join(KIT, "README.md")
PRE_MATCHER = "Edit|Write|NotebookEdit|MultiEdit|Bash|PowerShell"


def install_mod():
    import install as m
    return m


def fake_kit(parent, name, with_files=True):
    """A folder shaped like a kit (hooks\\pre_tool.py, hooks\\session.py); with_files False = a path that is not there."""
    kit = os.path.join(parent, name)
    if with_files:
        for n in ("pre_tool.py", "session.py"):
            write(os.path.join(kit, "hooks", n), "print()\n")
    return kit


def all_commands(data):
    return [h.get("command") for gs in (data.get("hooks") or {}).values() for g in gs for h in g.get("hooks", [])]


def n6_record(path, commands):
    """Put `commands` into the record of the installer for the settings file `path`, as `install` does it (the real
    function): the proof, for a later install, that these lines were written by it."""
    out = install_mod().record_installs(path, [("PreToolUse", "pre_tool.py", c) for c in commands])
    assert not any(x.startswith("WARNING") for x in out), out


def t_n4_command_runs_in_git_bash(t):
    """The command the installer writes, run by the SHELL Claude Code uses (Git Bash, `bash -c`): the file there -> python
    runs it, its stdin arrives and its exit code (2 = a block) is the exit code of the whole; the file gone -> exit 0 and a
    warning for the person (systemMessage) and for the agent (additionalContext). Folder names with a space, an apostrophe,
    brackets and a Cyrillic letter. The old form `python "<path>"` with the file gone -> exit 2 (the reason for all this)."""
    m = install_mod()
    if not m.find_bash():
        raise NotMeasured("Git Bash was not found: the command the installer writes was NOT run in the shell Claude Code uses")
    sb = sandbox("n4s")
    for name in ("plain", "with space", "it's (1)", "\u041f\u0430\u043f\u043a\u0430 \u00e9"):
        kit = os.path.join(sb, name)
        script = write(os.path.join(kit, "hooks", "pre_tool.py"),
                       "import sys\nd = sys.stdin.buffer.read()\nsys.stdout.write('GOT:' + str(len(d)))\nsys.exit(2)\n")
        for event in ("PreToolUse", "SessionStart", "SessionEnd"):
            cmd = m._build(script, event, os.path.join(sb, "proj", ".claude", "settings.json"))
            t.check("command [%s, %s]: written in a form this installer recognises, for this very path" % (name, event),
                    m.parse_command(cmd) == (script.replace("\\", "/"), "guarded"), cmd[:200])
            t.check("command [%s, %s]: no two backslashes in a row (they arrive as one through `bash -c`)" % (name, event),
                    "\\\\" not in cmd)
            rc, out, err = m._run_in_bash(cmd, b"12345")
            t.check("command [%s, %s]: the file is there -> python runs it, stdin arrives, exit code 2 comes out" % (name, event),
                    rc == 2 and out == "GOT:5", (rc, out[:120], err[:120]))
        # the file goes away (a rename: nothing is deleted)
        os.rename(script, script + ".gone")
        for event in ("PreToolUse", "SessionStart", "SessionEnd"):
            cmd = m._build(script, event, os.path.join(sb, "proj", ".claude", "settings.json"))
            rc, out, err = m._run_in_bash(cmd, b"12345")
            try:
                o = json.loads(out)
            except ValueError:
                o = {}
            said = o.get("systemMessage", "")
            ctx = (o.get("hookSpecificOutput") or {}).get("additionalContext", "")
            shown = script.replace("\\", "/")
            t.check("command [%s, %s]: the file is gone -> exit 0 and a valid JSON, the person is told where and that "
                    "the protection is OFF" % (name, event),
                    rc == 0 and shown in said and "OFF" in said and "settings.json" in said, (rc, out[:200], err[:120]))
            if event == "SessionEnd":
                t.check("command [%s, SessionEnd]: only a systemMessage (SessionEnd has no context for the agent)" % name,
                        "hookSpecificOutput" not in o, o)
            else:
                t.check("command [%s, %s]: the agent is told as well, with the leave to edit the settings" % (name, event),
                        (o.get("hookSpecificOutput") or {}).get("hookEventName") == event and shown in ctx
                        and "allowed to edit" in ctx, ctx[:200])
        old = 'python "%s"' % script
        rc, out, err = m._run_in_bash(old, b"{}")
        t.check("old form [%s]: the file is gone -> python exits with 2 (a block): the problem this form had" % name,
                rc == 2, (rc, out, err[:120]))


def t_n4_form_boundaries(t):
    """parse_command takes EXACTLY the forms this installer writes; is_ours on top of it keeps its earlier boundaries."""
    m = install_mod()
    own = m.hook_command("pre_tool.py", "PreToolUse", "S")
    t.check("form: the command of this kit is parsed and is ours", m.parse_command(own) == (
        norm(os.path.join(KIT, "hooks", "pre_tool.py")), "guarded") and m.is_ours(own) and m.is_ours(own, "pre_tool.py")
        and not m.is_ours(own, "session.py"))
    t.check("form: the old form of this kit is parsed (plain) and is ours",
            m.parse_command('python "%s"' % os.path.join(KIT, "hooks", "session.py"))[1] == "plain"
            and m.is_ours('python "%s"' % os.path.join(KIT, "hooks", "session.py")))
    other = m._build(os.path.join(KIT, "hooks", "pre_tool.py.backup"), "PreToolUse", "S")
    t.check("form: the same form naming `pre_tool.py.backup` is NOT ours", not m.is_ours(other))
    t.check("form: the command with a tail added is not parsed and not ours",
            m.parse_command(own + " --log x") is None and not m.is_ours(own + " --log x"))
    t.check("form: not a string / empty -> None, not ours", m.parse_command(None) is None and not m.is_ours(None)
            and m.parse_command("") is None)
    odd = "it's a $HOME `x` (1)"
    cmd = m._build(os.path.join(RUN, odd, "hooks", "session.py"), "SessionStart", "S")
    t.check("form: a path with an apostrophe, a dollar, a backtick and brackets survives the quoting both ways",
            m.parse_command(cmd) == (os.path.join(RUN, odd, "hooks", "session.py").replace("\\", "/"), "guarded"), cmd[:160])


def t_n4_dead_lines(t):
    """Item 4. A line is DEAD only when all three hold: exactly a form of this installer + a hook file name of the kit in
    a folder `hooks` + no file at that path. `install` takes out dead lines and names each; `uninstall` takes out those and
    its own. Controls: a living similar line (the file is there) is untouched; a dead line that fails any one of the three
    is untouched; a foreign hook in the same group stays. Called in-process (the mutations below patch the functions)."""
    m = install_mod()
    sb = sandbox("n4d")
    sp = os.path.join(sb, "proj", ".claude", "settings.json")
    gone = os.path.join(sb, "old_place")                      # never made
    gone2 = os.path.join(sb, "older_place")
    alive = fake_kit(sb, "other_copy_alive")
    foreign = {"type": "command", "command": "python C:/tools/mine.py"}

    def new(kit, script="pre_tool.py", event="PreToolUse"):
        return m._build(os.path.join(kit, "hooks", script), event, sp)

    def old(kit, script="pre_tool.py"):
        return 'python "%s"' % os.path.join(kit, "hooks", script)

    def grp(*cmds, matcher=None):
        g = {"hooks": [{"type": "command", "command": c, "timeout": 60} if isinstance(c, str) else c for c in cmds]}
        return dict([("matcher", matcher)] + list(g.items())) if matcher else g

    dead_cmds = [new(gone), old(gone2), new(gone, "session.py", "SessionStart"), old(gone2, "session.py")]
    living_cmds = [new(alive), old(alive), new(alive, "session.py", "SessionStart")]
    tampered = [                                           # dead by the missing file only: fail the form or the name
        old(gone) + " --log x",
        'python "%s"' % os.path.join(gone, "hooks", "other_tool.py"),
        'python "%s"' % os.path.join(gone, "pre_tool.py"),
        'bash wrapper.sh "%s"' % os.path.join(gone, "hooks", "pre_tool.py").replace("\\", "/"),
        "if [ -f '%s' ]; then python '%s'; else printf '%%s' '{\"systemMessage\": \"ShedKeeper\"}'; fi" % (
            os.path.join(gone, "hooks", "pre_tool.py").replace("\\", "/"), os.path.join(gone, "hooks", "session.py").replace("\\", "/")),
        "if [ -f '%s' ]; then python '%s'; else printf '%%s' '{\"systemMessage\": \"hello\"}'; fi" % (
            ((os.path.join(gone, "hooks", "pre_tool.py").replace("\\", "/"),) * 2)),
        'python "hooks\\pre_tool.py"',
    ]
    shared_foreign_cmd = foreign["command"]
    data = {"theme": "dark", "hooks": {
        "PreToolUse": [grp(dead_cmds[0], matcher=PRE_MATCHER), grp(dead_cmds[1], matcher=PRE_MATCHER),
                       grp(living_cmds[0], living_cmds[1], matcher=PRE_MATCHER),
                       grp(dict(foreign), dead_cmds[1], matcher="Bash")] + [grp(c) for c in tampered],
        "SessionStart": [grp(dead_cmds[2]), grp(living_cmds[2])],
        "SessionEnd": [grp(dead_cmds[3])],
        "Stop": [grp("python C:/x/stop.py")]}}
    # the record of what this installer wrote: the dead lines (it wrote them from another folder), the living ones, and
    # the look-alikes (recorded, but they fail the form or the name -> still not dead)
    n6_record(sp, dead_cmds + living_cmds + tampered)
    write(sp, json.dumps(data, indent=2))
    before = open(sp, "rb").read()
    rc, lines = m.install(sp)
    out = "\n".join(lines)
    after = jload(sp)
    cmds = all_commands(after)
    t.check("dead lines: install -> exit 0", rc == 0, out)
    t.check("dead lines: every dead line (of the new form and of the old) is gone",
            not any(c in cmds for c in dead_cmds), out)
    prefix = "removed a line of a previous installation from %s -- that folder no longer exists" % sp
    t.check("dead lines: each removal is named, one line each (5 dead lines: 4 in own groups, 1 in a shared group)",
            len([x for x in lines if x.startswith(prefix)]) == 5 and (gone2 in out or gone2.replace("\\", "/") in out), out)
    t.check("dead lines: the dead line in a group shared with a foreign hook went, the foreign hook stayed",
            shared_foreign_cmd in cmds and any(g["hooks"] == [foreign] for g in after["hooks"]["PreToolUse"]
                                                if isinstance(g, dict) and g.get("matcher") == "Bash"), out)
    t.check("dead lines: the LIVING lines of another copy of the kit (new and old form) are untouched",
            all(c in cmds for c in living_cmds), out)
    t.check("dead lines: the look-alikes that fail the form or the name stay, however dead their path (7 of them)",
            all(c in cmds for c in tampered), [c[:60] for c in tampered if c not in cmds])
    t.check("dead lines: the foreign settings (theme, Stop hook) are intact",
            after.get("theme") == "dark" and "python C:/x/stop.py" in cmds, after)
    t.check("dead lines: this kit's own three entries were added",
            len([c for c in cmds if m.is_ours(c)]) == 3, out)
    bk = [n for n in os.listdir(os.path.dirname(sp)) if "shedkeeper-backup" in n]
    t.check("dead lines: the settings were copied before the edit, and the copy is the old content",
            len(bk) == 1 and open(os.path.join(os.path.dirname(sp), bk[0]), "rb").read() == before, bk)
    rc, lines = m.install(sp)
    t.check("dead lines: a second install removes nothing and changes nothing",
            rc == 0 and not any(x.startswith("removed a line") for x in lines) and "nothing to change" in "\n".join(lines), lines)

    # uninstall: its own and the dead, not the living, not the foreign
    write(sp, json.dumps(data, indent=2))
    rc, lines = m.install(sp)
    rc, lines = m.uninstall(sp)
    out = "\n".join(lines)
    cmds = all_commands(jload(sp))
    t.check("dead lines: uninstall takes out the entries of this kit", not any(m.is_ours(c) for c in cmds), out)
    t.check("dead lines: uninstall keeps the living lines of another copy, the look-alikes and the foreign hooks",
            all(c in cmds for c in living_cmds + tampered) and shared_foreign_cmd in cmds and "python C:/x/stop.py" in cmds, out)
    write(sp, json.dumps(data, indent=2))
    rc, lines = m.uninstall(sp)
    cmds = all_commands(jload(sp))
    t.check("dead lines: uninstall alone (nothing of this kit in the file) takes out the dead lines and names them",
            rc == 0 and not any(c in cmds for c in dead_cmds)
            and len([x for x in lines if x.startswith("removed a line of a previous installation")]) == 5
            and all(c in cmds for c in living_cmds + tampered), "\n".join(lines))

    # a volume that cannot be reached is not 'a folder that is gone'
    letter = next((l for l in "ZYXWVUTSRQPONM" if not os.path.exists(l + ":\\")), None)
    if letter:
        far = letter + ":\\kit_on_unplugged_disk"
        n6_record(sp, [new(far)])
        write(sp, json.dumps({"hooks": {"PreToolUse": [grp(new(far), matcher=PRE_MATCHER)]}}))
        rc, lines = m.install(sp)
        t.check("dead lines: a line on a volume that cannot be reached is NOT removed, and the installer says so",
                new(far) in all_commands(jload(sp)) and any(x.startswith("kept a line of another installation") for x in lines), lines)
    else:
        t.skip("dead lines: unreachable volume", "no free drive letter")


def t_n4_old_form_upgrade_and_check(t):
    """The entries of 0.1.0 / 0.2.0 of THIS kit are rewritten into the new form by `install`; `check` says an entry is old,
    names the dead lines of a previous place (exit 1) and says the two probes (command, fallback). CLI, as a person runs it."""
    sb = sandbox("n4c")
    home = os.path.join(sb, "home")
    os.makedirs(home)
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    sp = os.path.join(proj, ".claude", "settings.json")
    gone = os.path.join(sb, "previous_place")
    m = install_mod()
    with env(SHEDKEEPER_USER_HOME=home, SHEDKEEPER_STORE_ROOT=os.path.join(sb, "store"), SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        rc, out = inst("install", "--project", proj)
        good = jload(sp)
        # 1. the entries as 0.2.0 wrote them
        olds = json.loads(json.dumps(good))
        for gs in olds["hooks"].values():
            for g in gs:
                for h in g["hooks"]:
                    h["command"] = 'python "%s"' % os.path.join(KIT, "hooks", "pre_tool.py" if "pre_tool.py" in h["command"].split(";")[0] else "session.py")
        write(sp, json.dumps(olds))
        rc, out = inst("check", "--project", proj)
        t.check("old form: check says the entries are of the old form and fails (a missing folder would BLOCK)",
                rc == 1 and "old form" in out, out)
        rc, out = inst("install", "--project", proj)
        new_cmds = all_commands(jload(sp))
        t.check("old form: install rewrites the entries of this kit into the new form, no duplicates",
                rc == 0 and out.count("entry updated (command") == 3 and len(new_cmds) == 3
                and all(m.parse_command(c)[1] == "guarded" for c in new_cmds), out)
        rc, out = inst("check", "--project", proj)
        t.check("old form: after that check is green; the two probes are named (the command in Git Bash, the fallback)",
                rc == 0 and "Git Bash" in out and "probe of the fallback" in out and out.count(": OK -- ") == 2, out)
        # 2. a previous place that is gone
        moved = json.loads(json.dumps(jload(sp)))
        moved["hooks"]["PreToolUse"].append({"matcher": PRE_MATCHER, "hooks": [
            {"type": "command", "command": 'python "%s"' % os.path.join(gone, "hooks", "pre_tool.py"), "timeout": 60}]})
        n6_record(sp, ['python "%s"' % os.path.join(gone, "hooks", "pre_tool.py")])      # the old place was installed by this installer
        write(sp, json.dumps(moved))
        rc, out = inst("check", "--project", proj)
        t.check("moved folder: check from the new place names the line of the old place whose path does not exist (and says "
                "it blocks, old form), exit 1",
                rc == 1 and os.path.join(gone, "hooks", "pre_tool.py") in out and "does not exist" in out and "BLOCKS" in out, out)
        rc, out = inst("install", "--project", proj)
        t.check("moved folder: install from the new place removes it and says so",
                rc == 0 and "removed a line of a previous installation from" in out and "no longer exists" in out
                and not any(gone in (c or "") for c in all_commands(jload(sp))), out)
        rc, out = inst("check", "--project", proj)
        t.check("moved folder: and check is green again", rc == 0, out)


def t_n4_agent_and_docs(t):
    """The agent is told how to remove ShedKeeper (<= 400 characters), the README says what the person and the agent need."""
    guide_src = read(os.path.join(KIT, "hooks", "session.py"))
    readme = read(README)
    sb = sandbox("n4g")
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    tp = write(os.path.join(sb, "cc", "proj-folder", "s1.jsonl"), "1\n")
    with env(SHEDKEEPER_STORE_ROOT=os.path.join(sb, "vol"), SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        rc, out, err = hook_sub(SESSION, {"hook_event_name": "SessionStart", "transcript_path": tp, "session_id": "s1", "cwd": proj})
        ctx = (one_json(out) or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        guide = ctx.partition(" WARNING: ")[0]
        t.check("guide: it tells the agent how to remove ShedKeeper -- uninstall, the kit folder to the trash, the store stays",
                'install.py uninstall --project <dir> (or --user)' in guide and "kit folder to the trash" in guide
                and "the store stays" in guide, guide)
        t.check("guide: still at most 400 characters with this kit's path", 0 < len(guide) <= 400, len(guide))
    sys.path.insert(0, os.path.join(KIT, "hooks"))
    import session as session_mod
    unusable = session_mod._guide(False)
    t.check("guide: the 'store not usable' variant carries the removal line as well and stays within 400 characters",
            "uninstall" in unusable and "the store stays" in unusable and len(unusable) <= 400, len(unusable))
    t.check("README, install: the shell of the hook is named (Git Bash, `bash -c`, bash 5.2.37 measured; the docs say `bash` by default)",
            "Git Bash" in readme and "bash -c" in readme and "5.2.37" in readme and '`shell` field' in readme, "missing")
    t.check("README: a 'Moving, removing and reinstalling' section with the four items (remove, folder gone, move, old entries)",
            "## Moving, removing and reinstalling" in readme and "Removing ShedKeeper" in readme
            and "If the folder is already gone" in readme and "Moving the folder" in readme and "Entries of 0.1.0 and 0.2.0" in readme, "missing")
    t.check("README: the promise changed -- a missing folder is a loud pass with the protection OFF, not 'everything is refused'",
            "protection is OFF" in readme and "NOT blocked" in readme, "missing")
    t.check("README: the dead-line rule (all four: recorded, form, name, proven absent) and the printed words",
            "all four hold" in readme and "removed a line of a previous installation from <settings file> -- that folder no longer exists" in readme
            and "did not write" in readme and "not known" in readme, "missing")
    t.check("README: the store stays after uninstall", "**The store stays**" in readme, "missing")
    ag = readme[readme.index("## For your agent"):readme.index("## Related tools")]
    t.check("README, for your agent: how to remove, and what to do on the 'was not found' message",
            "To remove ShedKeeper" in ag and "uninstall" in ag and "was not found at" in ag and "allowed to edit" in ag, ag[-600:])
    t.check("README: the old claim 'until you take the old entries out of the settings file by hand' is gone as the promise",
            "an installer only recognises entries that name its own folder" not in readme)


def t_mutations_n4(t):
    """removal and re-install: a guarded place is broken in memory and the matching test must turn red."""
    m = install_mod()

    def first_path(command):
        found = re.search(r"""[A-Za-z]:[\\/][^'"]*?\.py""", command or "")
        return found.group(0) if found else None

    def m_missing_only():               # a line is dead by ONE sign: the file is not there (form and name not looked at)
        def dead(command, recorded=None):
            p = first_path(command)
            return ("dead", p) if p and not os.path.exists(p) and not m.is_ours(command) else None
        return patched(m, "classify_dead", dead)

    def m_form_only():                  # ... by the form alone (the file is not looked at: a living line goes too)
        def dead(command, recorded=None):
            parsed = m.parse_command(command)
            return ("dead", parsed[0]) if parsed and not m.is_ours(command) else None
        return patched(m, "classify_dead", dead)

    def m_name_only():                  # ... by the name of the hook file alone
        def dead(command, recorded=None):
            p = first_path(command)
            return ("dead", p) if p and os.path.basename(p).lower() in m.HOOK_FILES and not m.is_ours(command) else None
        return patched(m, "classify_dead", dead)

    def m_no_file_check():              # the command in the settings does not look whether the file is there
        real = m._build

        def blind(hook, event, settings_file=None):
            return 'python "%s"' % hook
        return patched(m, "_build", blind)

    def m_silent_removal():             # dead lines are removed and not named
        return patched(m, "_dead_lines", lambda removed, path: [])

    def m_unplugged_is_gone():          # a volume that cannot be reached is taken for a folder that is gone
        real = m.classify_dead

        def dead(command, recorded=None):
            r = real(command, recorded)
            return ("dead", r[1]) if r and r[0] == "offline" else r
        return patched(m, "classify_dead", dead)

    def m_install_keeps_dead():         # install does not remove dead lines (the 0.2.0 behaviour)
        return patched(m, "_pick_dead", lambda recorded: (lambda command: None))


    run_mutation(t, "a line is judged dead by the missing file only", m_missing_only, [t_n4_dead_lines])
    run_mutation(t, "a line is judged dead by the form only (a living look-alike goes)", m_form_only, [t_n4_dead_lines])
    run_mutation(t, "a line is judged dead by the hook file name only", m_name_only, [t_n4_dead_lines])
    run_mutation(t, "the command in the settings does not look for the hook file", m_no_file_check, [t_n4_command_runs_in_git_bash])
    run_mutation(t, "dead lines are removed and not named", m_silent_removal, [t_n4_dead_lines])
    run_mutation(t, "an unreachable volume is taken for a gone folder", m_unplugged_is_gone, [t_n4_dead_lines])
    run_mutation(t, "install does not remove dead lines", m_install_keeps_dead, [t_n4_dead_lines])

# =============================================================================================
# 13. the plugin: ShedKeeper as a Claude Code plugin + its own marketplace; two doors, one handling
# =============================================================================================

PLUGIN_JSON = os.path.join(KIT, ".claude-plugin", "plugin.json")
MARKET_JSON = os.path.join(KIT, ".claude-plugin", "marketplace.json")
HOOKS_JSON = os.path.join(KIT, "hooks", "hooks.json")
ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"


def n5_json(path):
    return json.loads(read(path))


def n5_flat(hooks_obj):
    """{event: [(matcher, script basename, timeout, type)]} from a `hooks` object (a settings file or hooks.json)."""
    out = {}
    for event, groups in (hooks_obj or {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                cmd = h.get("command", "")
                found = re.findall(r"hooks[/\\]+(pre_tool\.py|session\.py)", cmd)
                out.setdefault(event, []).append((g.get("matcher"), found[0] if found else None, h.get("timeout"), h.get("type")))
    return out


def t_n5_files(t):
    """The three files of the plugin: forms, one version, the hooks equal to the installer's, and Claude Code's own validator."""
    m = install_mod()
    version = read(os.path.join(KIT, "VERSION")).strip()
    plug, market = n5_json(PLUGIN_JSON), n5_json(MARKET_JSON)
    t.check("plugin.json: name shedkeeper, the version of VERSION, Apache-2.0, author Serpentis-Git, a description",
            plug.get("name") == "shedkeeper" and plug.get("version") == version and plug.get("license") == "Apache-2.0"
            and (plug.get("author") or {}).get("name") == "Serpentis-Git" and plug.get("description"), plug)
    t.check("plugin.json: the name is the one install.py and the messages use (shedkeeper@shedkeeper)",
            plug.get("name") == m.PLUGIN and m.PLUGIN_ID == "%s@%s" % (plug.get("name"), market.get("name")), (m.PLUGIN_ID, market.get("name")))
    t.check("plugin.json: no component paths that would replace the default hooks/hooks.json",
            not any(k in plug for k in ("hooks", "skills", "commands", "agents")), sorted(plug))
    pl = market.get("plugins") or []
    t.check("marketplace.json: a name (kebab-case, not reserved), an owner, ONE plugin `shedkeeper` whose source is this repository",
            re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", market.get("name", "")) and (market.get("owner") or {}).get("name")
            and len(pl) == 1 and pl[0].get("name") == "shedkeeper" and pl[0].get("source") == "./", market)
    t.check("marketplace.json: the version is set in ONE place (plugin.json wins silently when it is in both)",
            "version" not in pl[0] and "version" not in (market.get("metadata") or {}), pl[0])
    hooks = n5_json(HOOKS_JSON)
    t.check("hooks.json: exactly what the installer would write (events, matcher, timeout, commands)",
            hooks == m.plugin_hooks(), "differs from install.plugin_hooks()")
    want = {"PreToolUse": [(PRE_MATCHER, "pre_tool.py", 60, "command")], "SessionStart": [(None, "session.py", 60, "command")],
            "SessionEnd": [(None, "session.py", 60, "command")]}
    t.check("hooks.json: the three events, the same tool matcher, timeout 60, the same two hook files (read from the file, "
            "not from the installer)", n5_flat(hooks["hooks"]) == want, n5_flat(hooks["hooks"]))
    sb = sandbox("n5f")
    path = os.path.join(sb, "proj", ".claude", "settings.json")
    os.makedirs(os.path.dirname(path))
    with env(SHEDKEEPER_USER_HOME=os.path.join(sb, "home")):
        m.install(path)
    t.check("hooks.json = the entries install.py writes into a settings file: the same events, matchers, timeouts, scripts",
            n5_flat(n5_json(path)["hooks"]) == n5_flat(hooks["hooks"]), (n5_flat(n5_json(path)["hooks"]), n5_flat(hooks["hooks"])))
    for event, groups in hooks["hooks"].items():
        for g in groups:
            for h in g["hooks"]:
                cmd = h["command"]
                script = re.search(r"hooks/(pre_tool\.py|session\.py)", cmd).group(1)
                t.check("hooks.json [%s]: the command runs %s/hooks/%s of the plugin root, with --via-plugin; the file is in the kit"
                        % (event, ROOT_VAR, script),
                        '"%s/hooks/%s"' % (ROOT_VAR, script) in cmd and cmd.count("--via-plugin") == 1
                        and os.path.isfile(os.path.join(KIT, "hooks", script)), cmd[:160])
                t.check("hooks.json [%s]: no two backslashes and no double quote inside the single-quoted warning (bash -c eats them)"
                        % event, "\\\\" not in cmd and '\\"' not in cmd)
    claude = shutil.which("claude")
    if not claude:
        t.skip("claude plugin validate", "claude is not on PATH")
        return
    r = subprocess.run([claude, "plugin", "validate", KIT, "--strict"], capture_output=True, timeout=120)
    outt = (r.stdout + r.stderr).decode("utf-8", "replace")
    t.check("claude plugin validate <kit> --strict: the marketplace passes (exit 0, no warning)", r.returncode == 0
            and "passed" in outt and "warning" not in outt.lower(), outt[-300:])
    pdir = os.path.join(sb, "plugin_only")
    write(os.path.join(pdir, ".claude-plugin", "plugin.json"), read(PLUGIN_JSON))
    write(os.path.join(pdir, "hooks", "hooks.json"), read(HOOKS_JSON))
    r = subprocess.run([claude, "plugin", "validate", pdir, "--strict"], capture_output=True, timeout=120)
    outt = (r.stdout + r.stderr).decode("utf-8", "replace")
    t.check("claude plugin validate <plugin folder> --strict: plugin.json and hooks/hooks.json pass", r.returncode == 0
            and "passed" in outt and "warning" not in outt.lower() and "error" not in outt.lower(), outt[-300:])
    write(os.path.join(pdir, "hooks", "hooks.json"), '{ "hooks": {')
    r = subprocess.run([claude, "plugin", "validate", pdir], capture_output=True, timeout=120)
    t.check("... and the validator does read hooks.json (a broken one fails): the green above is not a blind one", r.returncode != 0,
            (r.stdout + r.stderr).decode("utf-8", "replace")[-200:])


def n5_plugin_root(parent, name):
    """A folder shaped like an installed plugin whose pre_tool.py reports what it got. -> the root."""
    root = os.path.join(parent, name)
    write(os.path.join(root, "hooks", "pre_tool.py"),
          "import sys\nd = sys.stdin.buffer.read()\nsys.stdout.write('GOT:%d:%s' % (len(d), ','.join(sys.argv[1:])))\nsys.exit(2)\n")
    write(os.path.join(root, "hooks", "session.py"),
          "import sys\nd = sys.stdin.buffer.read()\nsys.stdout.write('GOT:%d:%s' % (len(d), ','.join(sys.argv[1:])))\nsys.exit(0)\n")
    return root


def t_n5_command_in_git_bash(t):
    """The plugin command, as Claude Code runs it (Git Bash, `bash -c`) with the root substituted inline -- once with
    forward slashes, once the Windows way (backslashes) -- and with the root left to the exported variable: the file is
    there -> python runs it, `--via-plugin` and stdin arrive, exit 2 stays 2; the file is gone -> exit 0 and a warning in
    plugin words."""
    m = install_mod()
    if not m.find_bash():
        raise NotMeasured("Git Bash was not found: the plugin command was NOT run in the shell Claude Code uses")
    sb = sandbox("n5c")
    for name in ("plain", "with space", "it's (1)", "Папка é"):
        root = n5_plugin_root(sb, name)
        forms = [("forward", root.replace("\\", "/")), ("windows", os.path.normpath(root))]
        for event, script in (("PreToolUse", "pre_tool.py"), ("SessionStart", "session.py"), ("SessionEnd", "session.py")):
            raw = m.plugin_command(script, event)
            code = 2 if script == "pre_tool.py" else 0
            for label, shown in forms:
                rc, out, err = m._run_in_bash(raw.replace(ROOT_VAR, shown), b"12345")
                t.check("plugin command [%s, %s, %s]: the file is there -> python runs it, stdin and --via-plugin arrive, exit %d stays"
                        % (name, event, label, code), rc == code and out == "GOT:5:--via-plugin", (rc, out[:120], err[:120]))
            with env(CLAUDE_PLUGIN_ROOT=os.path.normpath(root)):
                rc, out, err = m._run_in_bash(raw.replace(ROOT_VAR, "${CLAUDE_PLUGIN_ROOT}"), b"12345")
            t.check("plugin command [%s, %s]: with the root left to the exported variable (a Windows-style value) it works too"
                    % (name, event), rc == code and out == "GOT:5:--via-plugin", (rc, out[:120], err[:120]))
        # the root loses its files (a rename: nothing is deleted)
        os.rename(os.path.join(root, "hooks", "pre_tool.py"), os.path.join(root, "hooks", "pre_tool.py.gone"))
        os.rename(os.path.join(root, "hooks", "session.py"), os.path.join(root, "hooks", "session.py.gone"))
        for event, script in (("PreToolUse", "pre_tool.py"), ("SessionStart", "session.py"), ("SessionEnd", "session.py")):
            for label, shown in forms:
                rc, out, err = m._run_in_bash(m.plugin_command(script, event).replace(ROOT_VAR, shown), b"12345")
                try:
                    o = json.loads(out)
                except ValueError:
                    o = {}
                ctx = (o.get("hookSpecificOutput") or {}).get("additionalContext", "")
                t.check("plugin command [%s, %s, %s]: the file is gone -> exit 0, the person is told the protection is OFF and how "
                        "to reinstall / remove in /plugin words" % (name, event, label),
                        rc == 0 and "OFF" in o.get("systemMessage", "") and "/plugin uninstall shedkeeper@shedkeeper" in o.get("systemMessage", "")
                        and "/plugin install shedkeeper@shedkeeper" in o.get("systemMessage", ""), (rc, out[:200], err[:120]))
                if event == "SessionEnd":
                    t.check("plugin command [SessionEnd]: only a systemMessage", "hookSpecificOutput" not in o, o)
                else:
                    t.check("plugin command [%s, %s]: the agent is told as well (and not to edit the cache by hand)" % (event, label),
                            (o.get("hookSpecificOutput") or {}).get("hookEventName") == event and "OFF" in ctx and "cache" in ctx, ctx[:160])
        rc, out, err = m._run_in_bash('python "%s/hooks/pre_tool.py"' % forms[0][1], b"{}")
        t.check("control: a command without the file check, the file gone, exits 2 (a BLOCK) -- the reason for the check [%s]" % name, rc == 2, (rc, out[:80]))


def t_n15_no_python(t):
    """The command the installer writes when `python` cannot be started (bash: 127 not found, 126 the Microsoft Store
    placeholder / not executable): a loud warning for the person and the agent and exit 0 -- never a silent pass without
    a copy. Run in Git Bash like Claude Code does, for the settings form and the plugin form. Controls: a working python
    keeps its exit code (2 blocks, 1 stays 1, 0 stays 0) and prints only its own output; the form of 0.2.0 (no such
    branch) is still recognised as ours."""
    m = install_mod()
    if not m.find_bash():
        raise NotMeasured("Git Bash was not found: the command was NOT run in the shell Claude Code uses")
    sb = sandbox("n15")
    kit = os.path.join(sb, "kit")
    code_file = os.path.join(sb, "code.txt").replace("\\", "/")
    script = write(os.path.join(kit, "hooks", "pre_tool.py"),
                   "import sys\nd = sys.stdin.buffer.read()\nsys.stdout.write('GOT:' + str(len(d)))\n"
                   "sys.exit(int(open(%r).read()))\n" % code_file)
    root = os.path.join(sb, "plugin")
    write(os.path.join(root, "hooks", "pre_tool.py"), read(script))
    write(os.path.join(root, "hooks", "session.py"), read(script))
    sysdir = os.path.join(WINDIR, "System32")
    fake = {}
    for code in (126, 127):
        fake[code] = os.path.dirname(write(os.path.join(sb, "fake%d" % code, "python"), "#!/bin/sh\nexit %d\n" % code))
    # a stand-in `python` that runs the real interpreter, on a PATH that holds nothing else: a "working python"
    real = os.path.dirname(write(os.path.join(sb, "real", "python"), '#!/bin/sh\nexec "%s" "$@"\n' % PY.replace("\\", "/")))
    settings = os.path.join(sb, "proj", ".claude", "settings.json")

    def commands(event):
        return [("settings", m._build(script, event, settings)),
                ("plugin", m.plugin_command("pre_tool.py" if event != "SessionEnd" else "session.py", event).replace(
                    "${CLAUDE_PLUGIN_ROOT}", root.replace("\\", "/")))]

    def said(out):
        try:
            o = json.loads(out)
        except ValueError:
            return {}, "", ""
        return o, o.get("systemMessage", ""), (o.get("hookSpecificOutput") or {}).get("additionalContext", "")

    cases = [("no python on PATH", sysdir)] + [("python exits %d" % c, fake[c] + os.pathsep + sysdir) for c in (126, 127)]
    for event in ("PreToolUse", "SessionStart", "SessionEnd"):
        for door, cmd in commands(event):
            t.check("n15 [%s, %s]: no two backslashes in a row in the command (they arrive as one through `bash -c`)" % (door, event),
                    "\\\\" not in cmd)
            write(code_file, "2")
            for label, path in cases:
                with env(PATH=path):
                    rc, out, err = m._run_in_bash(cmd, b"12345")
                o, person, agent = said(out)
                t.check("n15 [%s, %s, %s]: exit 0 and a valid JSON; the person is told Python was not found and the protection is OFF"
                        % (door, event, label),
                        rc == 0 and "Python was not found" in person and "OFF" in person and "Microsoft Store" in person
                        and "python.org" in person and "restart" in person, (rc, out[:200], err[:100]))
                if event == "SessionEnd":
                    t.check("n15 [%s, SessionEnd, %s]: only a systemMessage" % (door, label), bool(o) and "hookSpecificOutput" not in o, o)
                else:
                    t.check("n15 [%s, %s, %s]: the agent is told to tell the person before changing or deleting files" % (door, event, label),
                            (o.get("hookSpecificOutput") or {}).get("hookEventName") == event and "OFF" in agent
                            and "before changing or deleting files" in agent, agent[:200])
            # control: a working python keeps its exit code and prints nothing but its own output
            for code in (2, 1, 0):
                write(code_file, str(code))
                with env(PATH=real + os.pathsep + sysdir):
                    rc, out, err = m._run_in_bash(cmd, b"12345")
                t.check("n15 control [%s, %s]: a working python exits %d -> the same code and its own output, no warning"
                        % (door, event, code), rc == code and out == "GOT:5", (rc, out[:120], err[:120]))
    # the file gone still says the other warning (the first branch is untouched)
    os.rename(script, script + ".gone")
    rc, out, err = m._run_in_bash(m._build(script, "PreToolUse", settings), b"1")
    t.check("n15: the hook file gone -> the lost-file warning, not the Python one", rc == 0 and "was not found at" in said(out)[1], (rc, out[:160]))
    os.rename(script + ".gone", script)
    # the form of 0.2.0: still ours
    person, agent = m.lost_messages(script, settings)
    lost = {"systemMessage": person, "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": agent}}
    p = m._sq(script.replace("\\", "/"))
    old = "if [ -f %s ]; then python %s; else printf '%%s' %s; fi" % (p, p, m._sq(json.dumps(lost, ensure_ascii=True)))
    new = m._build(script, "PreToolUse", settings)
    t.check("n15: the new form and the form of 0.2.0 are both parsed as guarded, with the same path",
            old != new and m.parse_command(new) == m.parse_command(old) == (script.replace("\\", "/"), "guarded"))
    kscript = os.path.join(KIT, "hooks", "pre_tool.py")
    kp = m._sq(kscript.replace("\\", "/"))
    kold = "if [ -f %s ]; then python %s; else printf '%%s' %s; fi" % (kp, kp, m._sq(json.dumps(lost, ensure_ascii=True)))
    knew = m.hook_command("pre_tool.py", "PreToolUse", settings)
    t.check("n15: both forms are ours for the kit they name; a tail or a changed exit is not",
            m.is_ours(knew, "pre_tool.py") and m.is_ours(kold, "pre_tool.py") and not m.is_ours(knew, "session.py")
            and m.parse_command(knew + " x") is None and m.parse_command(knew.replace("exit $rc", "exit 0")) is None
            and not m.is_ours(knew.replace("exit $rc", "exit 0"), "pre_tool.py"))
    hj = json.loads(read(HOOKS_JSON))
    t.check("n15: hooks.json carries the branch in every command",
            all("-eq 126" in h["command"] and "-eq 127" in h["command"] for gs in hj["hooks"].values() for g in gs for h in g["hooks"]))


def t_n15_install_upgrades_020(t):
    """`install` rewrites a settings entry of the 0.2.0 form (no 126/127 branch) into the new form and says so; `check`
    names the 0.2.0 form as old, and is green after. CLI, as a person runs it."""
    sb = sandbox("n15u")
    home = os.path.join(sb, "home")
    os.makedirs(home)
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    sp = os.path.join(proj, ".claude", "settings.json")
    m = install_mod()
    with env(SHEDKEEPER_USER_HOME=home, SHEDKEEPER_STORE_ROOT=os.path.join(sb, "store"), SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        rc, out = inst("install", "--project", proj)
        data = jload(sp)
        for gs in data["hooks"].values():
            for g in gs:
                for h in g["hooks"]:
                    h["command"] = _to_020(h["command"])
        write(sp, json.dumps(data))
        t.check("n15 upgrade: the entries are in the 0.2.0 form now (no branch)", all(
            "-eq 126" not in c and m.parse_command(c)[1] == "guarded" for c in all_commands(jload(sp))))
        rc, out = inst("check", "--project", proj)
        t.check("n15 upgrade: check names the 0.2.0 form and fails", rc == 1 and "0.2.0 form" in out, out)
        rc, out = inst("install", "--project", proj)
        cmds = all_commands(jload(sp))
        t.check("n15 upgrade: install rewrites the three entries into the new form, no duplicates",
                rc == 0 and out.count("entry updated (command") == 3 and len(cmds) == 3
                and all("-eq 126" in c and "-eq 127" in c for c in cmds), out)
        rc, out = inst("check", "--project", proj)
        t.check("n15 upgrade: after that check is green", rc == 0 and "0.2.0 form" not in out, out)


def _to_020(command):
    """The 0.2.0 form of a command of the new form: the branch after `python P;` is cut out."""
    head, tail = command.split("; rc=$?;", 1)
    return head + "; else" + tail.split("; else", 1)[1]


# ---- two doors, one handling (in this process, so that a mutation can be seen)

def n5_run(module, data, via_plugin, raw_argv0="hook.py", project="auto"):
    """Run a hook in this process. `project`: what Claude Code gives the hook as CLAUDE_PROJECT_DIR -- "auto" = the variable
    already set, else the cwd of the event (the project root, in these tests); None = the variable is NOT there."""
    old_in, buf = sys.stdin, io.StringIO()
    sys.stdin = _Stdin(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    if project == "auto":
        project = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd")
    try:
        with env(CLAUDE_PROJECT_DIR=project), contextlib.redirect_stdout(buf), \
                patched(sys, "argv", [raw_argv0] + (["--via-plugin"] if via_plugin else [])):
            rc = module.main()
    finally:
        sys.stdin = old_in
    return rc, buf.getvalue()


def n5_session_module():
    p = os.path.join(KIT, "hooks")
    if p not in sys.path:
        sys.path.insert(0, p)
    import session as session_mod
    return session_mod


def n5_project(sb, name, door):
    """A project folder; door: 'settings' (install.py entries of this kit, alive), 'none', 'dead' (entries of a kit folder that is
    gone), 'narrow' (alive but a narrower tool matcher), 'user' (the entries in the USER settings)."""
    m = install_mod()
    proj = os.path.join(sb, name)
    os.makedirs(os.path.join(proj, ".claude"), exist_ok=True)
    spath = os.path.join(proj, ".claude", "settings.json")
    home = os.path.join(sb, "home")
    os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
    if door in ("settings", "narrow"):
        m.install(spath)
        if door == "narrow":
            d = n5_json(spath)
            d["hooks"]["PreToolUse"][0]["matcher"] = "Edit"
            write(spath, json.dumps(d, indent=2))
    elif door == "dead":
        gone = fake_kit(sb, "gone_kit_" + name, with_files=False)
        d = {"hooks": {}}
        for event, matcher, script in m.ENTRIES:
            g = {"hooks": [{"type": "command", "command": m._build(os.path.join(gone, "hooks", script), event, spath), "timeout": 60}]}
            if matcher:
                g["matcher"] = matcher
            d["hooks"].setdefault(event, []).append(g)
        write(spath, json.dumps(d, indent=2))
    elif door == "user":                # the entries live in the user settings of ANOTHER home: the caller points SHEDKEEPER_USER_HOME at it
        uhome = os.path.join(sb, "home_user")
        os.makedirs(os.path.join(uhome, ".claude"), exist_ok=True)
        with env(SHEDKEEPER_USER_HOME=uhome):
            m.install(os.path.join(uhome, ".claude", "settings.json"))
    return proj


def copy_kit(target):
    """A copy of the kit as it is shipped: no tests, no caches, and NO `.git` -- a person who got the kit through `git clone` has
    one, and its objects are read-only files (copying them over an existing copy is `Permission denied`)."""
    shutil.copytree(KIT, target, ignore=shutil.ignore_patterns(".git", "tests", "__pycache__", ".pytest_cache", "*.pyc"), dirs_exist_ok=True)


def n6_plugin_installed(home, key="shedkeeper@shedkeeper", real=False, version="0.2.0", claude_dir=None, plugins_root=None, directory=None):
    """An INSTALLED plugin the way Claude Code keeps it: a folder in the plugin cache, an entry for `key` in `installed_plugins.json`
    and the marketplace in `known_marketplaces.json`, both in the plugins root (`home`/.claude/plugins, or `claude_dir`/plugins, or `plugins_root`).
    real=True copies this kit into the folder (its hooks run); else only the three files the proof asks for. directory=PATH: the marketplace was
    added from that folder (source `directory`, which Claude Code runs the plugin from in place) and the files are put THERE, the cache stays
    as the registry's installPath. -> the folder in the cache."""
    m = install_mod()
    claude = claude_dir or os.path.join(home, ".claude")
    pl = plugins_root or os.path.join(claude, "plugins")
    name, market = key.split("@")
    root = os.path.join(pl, "cache", market, name, version)
    for target in (root, directory) if directory else (root,):
        if real:
            copy_kit(target)
        else:
            for rel in m.PLUGIN_FILES:
                write(os.path.join(target, *rel.split("/")), "x\n")
    reg_path = os.path.join(pl, "installed_plugins.json")
    reg = jload(reg_path) if os.path.exists(reg_path) else {"version": 2, "plugins": {}}
    reg["plugins"][key] = [{"scope": "user", "installPath": root, "version": version}]
    write(reg_path, json.dumps(reg, indent=2))
    km_path = os.path.join(pl, "known_marketplaces.json")
    km = jload(km_path) if os.path.exists(km_path) else {}
    src = {"source": "directory", "path": directory} if directory else {"source": "github", "repo": "Serpentis-Git/shedkeeper"}
    km[market] = {"source": src, "installLocation": directory or os.path.join(pl, "marketplaces", market), "lastUpdated": "2026-10-01T00:00:00Z"}
    write(km_path, json.dumps(km, indent=2))
    return root


def n5_pre_event(proj, sb, kind):
    if kind == "rm":
        f = write(os.path.join(proj, "victim.txt"), "keep me\n")
        return bash_event("rm '%s'" % f.replace("\\", "/"), proj), f
    f = write(os.path.join(proj, "doc.md"), "before\n")
    return edit_event(f, cwd=proj), f


def t_n5_two_doors_runtime(t):
    """With the plugin hooks started by --via-plugin: silent while a LIVE entry of install.py covers the event; working in every
    other state (plugin alone, a dead entry, a narrower matcher, another project, ...). Without the argument: always working.
    Never two handlings, never none."""
    pm = pre_tool
    sm = n5_session_module()
    sb = sandbox("n5d")
    home = os.path.join(sb, "home")
    with store_env(sb) as (root, st), env(SHEDKEEPER_USER_HOME=home, CLAUDE_PROJECT_DIR=None):
        # --- PreToolUse
        proj = n5_project(sb, "both", "settings")
        ev, f = n5_pre_event(proj, sb, "rm")
        rc, out = n5_run(pm, ev, True)
        t.check("both doors, deletion: the hook of the plugin is SILENT (exit 0, no output) -- install.py's entries handle it",
                rc == 0 and out == "", (rc, out[:160]))
        rc, out = n5_run(pm, ev, False)
        t.check("both doors, deletion: the hook of install.py works: it refuses with the trash command", verdict(out) == "deny", out[:160])
        t.check("both doors, deletion: exactly ONE refusal for the one action (the two hooks run, as Claude Code would)",
                [verdict(n5_run(pm, ev, v)[1]) for v in (True, False)].count("deny") == 1)
        ev, f = n5_pre_event(proj, sb, "edit")
        rc, out = n5_run(pm, ev, True)
        t.check("both doors, edit: the plugin hook is silent and makes NO copy (it is not its turn)", out == "" and not copies_of(root, "doc"),
                (out[:80], copies_of(root, "doc")))
        rc, out = n5_run(pm, ev, False)
        t.check("both doors, edit: install.py's hook makes the copy", len(copies_of(root, "doc")) == 1, copies_of(root, "doc"))
        cases = [("none", "the plugin alone"), ("dead", "an entry of install.py whose kit folder is gone"),
                 ("narrow", "an entry of install.py with a narrower tool matcher")]
        for door, label in cases:
            proj = n5_project(sb, "p_" + door, door)
            ev, f = n5_pre_event(proj, sb, "rm")
            rc, out = n5_run(pm, ev, True)
            t.check("%s, deletion: the plugin hook WORKS (a refusal with the trash command)" % label, verdict(out) == "deny", (rc, out[:160]))
            ev, f = n5_pre_event(proj, sb, "edit")
            n_before = len(copies_of(root, "doc"))
            n5_run(pm, ev, True)
            t.check("%s, edit: the plugin hook makes the copy" % label, len(copies_of(root, "doc")) == n_before + 1,
                    (n_before, copies_of(root, "doc")))
        proj = n5_project(sb, "p_other", "none")
        other = n5_project(sb, "elsewhere", "settings")           # a live install in ANOTHER project does not cover this one
        ev, f = n5_pre_event(proj, sb, "rm")
        t.check("a live install.py entry in ANOTHER project does not silence the plugin here", verdict(n5_run(pm, ev, True)[1]) == "deny")
        proj = n5_project(sb, "p_user", "user")
        ev, f = n5_pre_event(proj, sb, "rm")
        with env(SHEDKEEPER_USER_HOME=os.path.join(sb, "home_user")):
            t.check("install.py's entries in the USER settings cover every project: the plugin is silent", n5_run(pm, ev, True)[1] == "")
        t.check("... and with the other user home (no entries there) the plugin works", verdict(n5_run(pm, ev, True)[1]) == "deny")
        # cwd inside a folder of the project: CLAUDE_PROJECT_DIR names the root; without it only the cwd is looked at (the safe side)
        proj = n5_project(sb, "p_sub", "settings")
        sub = os.path.join(proj, "sub")
        os.makedirs(sub)
        ev, f = n5_pre_event(sub, sb, "rm")
        with env(CLAUDE_PROJECT_DIR=proj):
            t.check("cwd in a subfolder + CLAUDE_PROJECT_DIR = the project: the plugin sees the entries of the project and is silent",
                    n5_run(pm, ev, True)[1] == "")
        ev, f = n5_pre_event(sub, sb, "rm")
        t.check("cwd in a subfolder, no CLAUDE_PROJECT_DIR: not decidable -> the plugin does NOT yield (a doubled hook, never a silent pair)",
                verdict(n5_run(pm, ev, True, project=None)[1]) == "deny")
        # the argument decides: without it the hook never yields
        proj = n5_project(sb, "p_arg", "settings")
        ev, f = n5_pre_event(proj, sb, "rm")
        t.check("started WITHOUT --via-plugin (the entries of install.py) the hook always works", verdict(n5_run(pm, ev, False)[1]) == "deny")
        # a check that fails never silences
        ev, f = n5_pre_event(proj, sb, "rm")
        with set_attr(install_mod(), "live_entries", lambda project=None: 1 / 0):
            t.check("the check itself fails -> the plugin hook works (never a silent pair)", verdict(n5_run(pm, ev, True)[1]) == "deny")
        # --- SessionStart / SessionEnd
        tp = write(os.path.join(sb, "cc", "projects", "pf", "s1.jsonl"), "1\n2\n")
        for door, silent in (("settings", True), ("none", False)):
            proj = n5_project(sb, "s_" + door, door)
            for event in ("SessionStart", "SessionEnd"):
                dst = os.path.join(root, "transcripts", "pf", "s1.jsonl")
                if os.path.exists(dst):
                    os.rename(dst, dst + ".moved%d" % _counter[0])
                _counter[0] += 1
                data = {"hook_event_name": event, "transcript_path": tp, "session_id": "s1", "cwd": proj}
                rc, out = n5_run(sm, data, True)
                if silent:
                    t.check("%s, both doors: the plugin's session hook is silent and saves nothing" % event,
                            rc == 0 and out == "" and not os.path.exists(dst), (out[:100], os.path.exists(dst)))
                else:
                    o = parse(out) or {}
                    ctx = (o.get("hookSpecificOutput") or {}).get("additionalContext", "")
                    t.check("%s, the plugin alone: the session hook works (transcript saved%s)" % (event, "; the guide at the start" if event == "SessionStart" else ""),
                            rc == 0 and os.path.isfile(dst) and (event == "SessionEnd" or ("ShedKeeper (plugin) is on" in ctx)), (out[:200], os.path.isfile(dst)))
                    if event == "SessionStart":
                        guide = ctx.partition(" WARNING: ")[0]
                        t.check("the plugin guide: how to remove (/plugin ...) and the other way, the store stays, at most 400 characters",
                                "/plugin uninstall shedkeeper@shedkeeper" in guide and "install.py uninstall" in guide
                                and "the store stays" in guide and 0 < len(guide) <= 400, len(guide))
                        t.check("the plugin alone: no false 'not found in the settings' warning (the hooks run from the plugin)",
                                "not found in the settings" not in ctx, ctx[-240:])
        sm.KIT_SAVED = sm.KIT
        try:
            for k in ("C:/Users/Alex/.claude/plugins/cache/shedkeeper/shedkeeper/0.2.0",
                      "C:/Users/LongUserName/.claude/plugins/cache/shedkeeper/shedkeeper/10.12.3"):
                sm.KIT = k
                for usable in (True, False):
                    g = sm._guide(usable, True)
                    t.check("the plugin guide with a real cache path (%d characters), store %s: at most 400 characters, still names the /plugin removal"
                            % (len(k), "usable" if usable else "NOT usable"), len(g) <= 400 and "/plugin uninstall shedkeeper@shedkeeper" in g, len(g))
        finally:
            sm.KIT = sm.KIT_SAVED
        proj = n5_project(sb, "s_both_install_door", "settings")
        with env(CLAUDE_PROJECT_DIR=None):
            plug_state = os.path.join(proj, ".claude", "settings.local.json")
            write(plug_state, json.dumps({"enabledPlugins": {"shedkeeper@shedkeeper": True}}))
            ev_start = {"hook_event_name": "SessionStart", "transcript_path": tp, "session_id": "s1", "cwd": proj}
            rc, out = n5_run(sm, ev_start, False)
            ctx = ((parse(out) or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")
            t.check("install.py's session hook, the plugin only WRITTEN as enabled (not installed): no 'TWO doors' warning -- the plugin "
                    "is not shown to run", "also enabled as a plugin" not in ctx and "ShedKeeper is on (K=" in ctx, ctx[-300:])
            n6_plugin_installed(home)
            rc, out = n5_run(sm, ev_start, False)
            ctx = ((parse(out) or {}).get("hookSpecificOutput") or {}).get("additionalContext", "")
            t.check("install.py's session hook, the plugin also enabled AND installed: it says so (TWO doors) and names how to keep one",
                    "also enabled as a plugin" in ctx and "/plugin uninstall shedkeeper@shedkeeper" in ctx, ctx[-300:])
            t.check("... and still gives the normal (non-plugin) guide", "ShedKeeper is on (K=" in ctx, ctx[:120])


def t_n5_installer_refuses(t):
    """install.py does not put in a second way while the plugin is enabled for the target; nothing is touched; the scope
    is right (another project's plugin is no reason); `false` is not enabled; uninstall is never refused and says what remains."""
    m = install_mod()
    sb = sandbox("n5i")
    home = os.path.join(sb, "home")
    os.makedirs(os.path.join(home, ".claude"))
    proj = os.path.join(sb, "proj")
    os.makedirs(os.path.join(proj, ".claude"))
    other = os.path.join(sb, "other")
    os.makedirs(os.path.join(other, ".claude"))

    def run(argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = m.main(argv)
        return rc, buf.getvalue()

    def files(d):
        return sorted(os.listdir(d))

    spath = os.path.join(proj, ".claude", "settings.json")
    with env(SHEDKEEPER_USER_HOME=home, CLAUDE_PROJECT_DIR=None):
        n6_plugin_installed(home)                       # the plugin is really installed (two marketplaces' worth of keys)
        n6_plugin_installed(home, "shedkeeper@m2")
        # the plugin enabled in the project settings
        write(spath, json.dumps({"enabledPlugins": {"shedkeeper@shedkeeper": True}, "model": "x"}, indent=2))
        before = read(spath)
        rc, out = run(["install", "--project", proj])
        t.check("project: the plugin is enabled in the project settings -> install is REFUSED (exit 1) and says why and what to do",
                rc == 1 and "REFUSED" in out and "already on as a plugin" in out and "/plugin uninstall shedkeeper@shedkeeper" in out, (rc, out[:300]))
        t.check("... nothing was touched: the settings are byte for byte the same and no backup / temporary file appeared",
                read(spath) == before and files(os.path.dirname(spath)) == ["settings.json"], files(os.path.dirname(spath)))
        write(spath, json.dumps({"enabledPlugins": {"shedkeeper@shedkeeper": False}}, indent=2))
        rc, out = run(["install", "--project", proj])
        t.check("project: the plugin is listed but `false` (disabled) -> install goes", rc == 0 and "added" in out, (rc, out[:200]))
        run(["uninstall", "--project", proj])
        write(spath, json.dumps({"enabledPlugins": {"other-tool@shedkeeper": True, "shedkeeper-extras@x": True}}, indent=2))
        rc, out = run(["install", "--project", proj])
        t.check("project: another plugin (even a similar name) is not ShedKeeper -> install goes", rc == 0 and "added" in out, (rc, out[:200]))
        run(["uninstall", "--project", proj])
        # the plugin enabled for ANOTHER project only
        write(os.path.join(other, ".claude", "settings.json"), json.dumps({"enabledPlugins": {"shedkeeper@shedkeeper": True}}))
        rc, out = run(["install", "--project", proj])
        t.check("project: the plugin is enabled in ANOTHER project only -> install goes (the scope is read, not guessed)", rc == 0 and "added" in out, (rc, out[:200]))
        run(["uninstall", "--project", proj])
        # the plugin in the local settings of the project, and in the user settings
        write(os.path.join(proj, ".claude", "settings.local.json"), json.dumps({"enabledPlugins": {"shedkeeper@m2": True}}))
        rc, out = run(["install", "--project", proj])
        t.check("project: enabled in settings.local.json (any marketplace name) -> refused", rc == 1 and "shedkeeper@m2" in out, (rc, out[:200]))
        os.rename(os.path.join(proj, ".claude", "settings.local.json"), os.path.join(proj, ".claude", "settings.local.json.moved"))
        write(os.path.join(home, ".claude", "settings.json"), json.dumps({"enabledPlugins": {"shedkeeper@shedkeeper": True}}))
        rc, out = run(["install", "--project", proj])
        t.check("project: enabled for the USER (covers every project) -> refused", rc == 1 and "REFUSED" in out, (rc, out[:200]))
        rc, out = run(["install", "--user"])
        t.check("user: enabled for the user -> refused", rc == 1 and "REFUSED" in out, (rc, out[:200]))
        # uninstall is never refused and names what remains
        rc, out = run(["uninstall", "--project", proj])
        t.check("uninstall is never refused; it says that the plugin is still on and how to take it out",
                rc == 0 and "still on as a plugin" in out and "/plugin uninstall shedkeeper@shedkeeper" in out, (rc, out[:300]))


def t_n5_doors_named(t):
    """`install.py check` and `shedkeeper.py status` say which door is open: plugin / install.py / both / none."""
    m = install_mod()
    sb = sandbox("n5n")
    home = os.path.join(sb, "home")
    os.makedirs(os.path.join(home, ".claude"))
    cases = {}
    with store_env(sb) as (root, st), env(SHEDKEEPER_USER_HOME=home, CLAUDE_PROJECT_DIR=None):
        proj = os.path.join(sb, "proj")
        os.makedirs(os.path.join(proj, ".claude"))
        spath = os.path.join(proj, ".claude", "settings.json")

        def check_out(extra=()):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = m.main(["check", "--project", proj] + list(extra))
            return rc, buf.getvalue()

        def status_out():
            r = subprocess.run([PY, SHEDKEEPER_PY, "status"], capture_output=True, cwd=proj, timeout=60,
                               env=dict(clean_env(), SHEDKEEPER_USER_HOME=home, SHEDKEEPER_STORE_ROOT=os.path.join(sb, "store"),
                                        SHEDKEEPER_INI=os.path.join(sb, "no_such.ini")))
            return r.stdout.decode("utf-8", "replace")

        rc, out = check_out()
        t.check("none: check says `door: none` (and fails, as before: nothing is installed)", "door: none" in out and rc == 1, (rc, out[-300:]))
        t.check("none: status says `door: none`", "door: none" in status_out())
        write(os.path.join(proj, ".claude", "settings.local.json"), json.dumps({"enabledPlugins": {"shedkeeper@shedkeeper": True}}))
        rc, out = check_out()
        t.check("plugin written as enabled but NOT installed: check says the door is NOT PROVEN and fails (no green over a plugin that is not there)",
                "plugin door NOT PROVEN" in out and "door: none" in out and rc == 1, (rc, out[-500:]))
        plugin_dir = n6_plugin_installed(home, real=True)
        rc, out = check_out()
        t.check("plugin only: check says `door: the plugin`, reads hooks.json / plugin.json, runs the plugin command in Git Bash, and does NOT "
                "complain that install.py's entries are missing", "door: the plugin" in out and "hooks/hooks.json: the hooks of the plugin are the hooks of the installer" in out
                and "probe (the plugin's pre_tool command" in out and "NOT installed" not in out, out[-900:])
        t.check("plugin only: check exits 0 when the files, the probe and the store are fine", rc == 0, (rc, out[-500:]))
        st = status_out()
        t.check("plugin only: status says `door: the plugin` and does not print a misleading `pre_tool NO`", "door: the plugin" in st and "pre_tool NO" not in st, st[-400:])
        # both: put the entries in directly (install.py itself refuses: a hand edit, or the plugin enabled later)
        with env(SHEDKEEPER_USER_HOME=os.path.join(sb, "home2")):
            os.makedirs(os.path.join(sb, "home2", ".claude"))
            m.install(spath)
        rc, out = check_out()
        t.check("both: check says TWO DOORS, says the plugin hooks stay silent, names how to keep one; the exit code is the one of the "
                "install.py check (the state is safe)", "TWO DOORS" in out and "stay silent" in out and "/plugin uninstall shedkeeper@shedkeeper" in out
                , (rc, out[-700:]))
        t.check("both: status says TWO DOORS", "TWO DOORS" in status_out())
        os.rename(os.path.join(proj, ".claude", "settings.local.json"), os.path.join(proj, ".claude", "settings.local.json.moved"))
        rc, out = check_out()
        t.check("install.py only: check says `door: install.py`", "door: install.py" in out, out[-300:])
        t.check("install.py only: status says `door: install.py`", "door: install.py" in status_out())


def t_n5_docs(t):
    """README: the plugin way FIRST with the real names; the honest limits; removal; the agent's lines. The guide's wording."""
    readme = read(README)
    i1, i2 = readme.find("### 1. As a Claude Code plugin"), readme.find("### 2. With `install.py`")
    t.check("README, install: the plugin way is FIRST and install.py is second", 0 < i1 < i2, (i1, i2))
    way1 = readme[i1:i2]
    t.check("README: the two commands with the real names", "/plugin marketplace add Serpentis-Git/ShedKeeper" in way1
            and "/plugin install shedkeeper@shedkeeper" in way1, way1[:300])
    flat = " ".join(way1.split())
    t.check("README: honest -- while the repository is closed the first command works only with access; a local folder works; tokens for background updates",
            "While the repository is closed, the first command works only for someone who has access to it" in flat
            and "a local folder works as a marketplace" in flat and "GITHUB_TOKEN" in flat, flat[:700])
    t.check("README: the cache copy and shedkeeper.ini are named with the way out (SHEDKEEPER_INI); the update rule (version)",
            "plugin cache" in flat and "SHEDKEEPER_INI" in flat and "new version number" in flat, "missing")
    t.check("README: removal in /plugin words, the store stays; both ways -> nothing runs twice, and the printed words of `door`",
            "/plugin uninstall shedkeeper@shedkeeper" in flat and "The store stays" in flat and "TWO DOORS" in flat
            and "refuses while the plugin is enabled" in flat and "stay silent" in flat, flat[-900:])
    t.check("README: it says what the live stand checked (2026-10-01) and what was not", "Checked on a stand with a live agent on 2026-10-01" in flat and "Not checked:" in flat, "missing")
    ag = readme[readme.index("## For your agent"):readme.index("## Related tools")]
    t.check("README, for your agent: the plugin removal and the plugin 'not found' message",
            "/plugin uninstall shedkeeper@shedkeeper" in ag and "ShedKeeper plugin files were not found" in ag and "ShedKeeper (plugin)" in ag, ag[-700:])
    mv = readme[readme.index("## Moving, removing and reinstalling"):readme.index("## Find and bring back")]
    t.check("README, removing: the plugin way with the 7-day orphan folder and the honest 'not measured' about a vanished cache folder",
            "Removing ShedKeeper, the plugin way" in mv and "7 days" in mv and "not in its documentation" in mv, mv[:400])
    t.check("README: the old install.py text is still there (the other way is not lost)",
            "python install.py install --project" in readme and "Removing ShedKeeper (the `install.py` way)" in mv)


def t_mutations_n15(t):
    """The command for a Python that cannot start: a guarded place is broken in memory and the tests must turn red."""
    m = install_mod()
    real = m._guarded

    def m_no_branch():                  # the branch is gone: the form of 0.2.0 (a silent pass without a copy)
        return patched(m, "_guarded", lambda f, r, e, lost: "if [ -f %s ]; then python %s; else printf '%%s' %s; fi" % (f, r, lost))

    def m_only_127():                   # the Microsoft Store placeholder (126) is not covered
        return patched(m, "_guarded", lambda f, r, e, lost: real(f, r, e, lost).replace("[ $rc -eq 126 ] || ", ""))

    def m_exit_always_0():              # the exit code of python is lost: a block (2) no longer blocks
        return patched(m, "_guarded", lambda f, r, e, lost: real(f, r, e, lost).replace("exit $rc;", "exit 0;"))

    def m_warns_always():               # every non-zero code is read as "no python": a block becomes a warning
        return patched(m, "_guarded", lambda f, r, e, lost: real(f, r, e, lost).replace("[ $rc -eq 126 ] || [ $rc -eq 127 ]", "[ $rc -ne 0 ]"))

    run_mutation(t, "the 126/127 branch is removed (a silent pass without a copy)", m_no_branch, [t_n15_no_python])
    run_mutation(t, "only 127 is handled (the Store placeholder, 126, passes silently)", m_only_127, [t_n15_no_python])
    run_mutation(t, "the exit code of python is replaced by 0 (a block stops blocking)", m_exit_always_0, [t_n15_no_python])
    run_mutation(t, "every non-zero code is read as `no python` (a block turns into a warning)", m_warns_always, [t_n15_no_python])


def t_mutations_n5(t):
    """the plugin: a guarded place is broken in memory and the matching test must turn red."""
    m = install_mod()

    def m_both_run():                   # the plugin never yields: both doors handle every action
        return patched(m, "plugin_yields", lambda event, cwd=None: [])

    def m_silent_pair():                # the plugin always yields: with no install.py at all nobody handles anything
        return patched(m, "plugin_yields", lambda event, cwd=None: ["x"])

    def m_dead_counts():                # an entry whose kit folder is gone counts as covering (the file is not looked at)
        def live(project=None):
            res = {}
            for p in m.applicable_settings(project):
                data, err = m._load(p)
                for event, matcher, script in m.ENTRIES:
                    for g in ((data or {}).get("hooks") or {}).get(event, []):
                        for h in g.get("hooks", []):
                            if m.parse_command(h.get("command")) and (not matcher or g.get("matcher") == matcher):
                                res.setdefault(event, []).append(p)
            return res
        return patched(m, "live_entries", live)

    def m_matcher_ignored():            # a narrower tool matcher counts as covering
        def live(project=None):
            res = {}
            for p in m.applicable_settings(project):
                data, err = m._load(p)
                for event, matcher, script in m.ENTRIES:
                    for g in ((data or {}).get("hooks") or {}).get(event, []):
                        for h in g.get("hooks", []):
                            parsed = m.parse_command(h.get("command"))
                            if parsed and os.path.isfile(parsed[0]):
                                res.setdefault(event, []).append(p)
            return res
        return patched(m, "live_entries", live)

    def m_user_only_project():          # the user settings are not looked at
        return patched(m, "applicable_settings", lambda project=None: [os.path.join(project, ".claude", n) for n in ("settings.json", "settings.local.json")] if project else [])

    def m_installer_no_refusal():       # install does not look for the plugin
        return patched(m, "plugin_enabled", lambda paths: [])

    def m_any_plugin_flag():            # `false` counts as enabled
        def enabled(paths):
            out = []
            for p in paths:
                data, err = m._load(p)
                for key in ((data or {}).get("enabledPlugins") or {}):
                    if key.split("@")[0] == m.PLUGIN:
                        out.append((p, key))
            return out
        return patched(m, "plugin_enabled", enabled)

    def m_no_fallback():                # the plugin command has no look at the file
        return patched(m, "plugin_command", lambda script, event: 'python "%s/hooks/%s" --via-plugin' % (ROOT_VAR, script))

    def m_timeout_drift():              # the installer's timeout is not the plugin's
        return patched(m, "TIMEOUT_S", 30)

    run_mutation(t, "both doors work at the same time (the plugin never yields)", m_both_run, [t_n5_two_doors_runtime])
    run_mutation(t, "the plugin always yields (a silent pair: nobody handles)", m_silent_pair, [t_n5_two_doors_runtime])
    run_mutation(t, "an entry whose kit folder is gone silences the plugin", m_dead_counts, [t_n5_two_doors_runtime])
    run_mutation(t, "a narrower tool matcher silences the plugin", m_matcher_ignored, [t_n5_two_doors_runtime])
    run_mutation(t, "the user settings are not looked at for the entries of install.py", m_user_only_project, [t_n5_two_doors_runtime])
    run_mutation(t, "install.py puts itself in while the plugin is enabled", m_installer_no_refusal, [t_n5_installer_refuses])
    run_mutation(t, "a plugin set to false counts as enabled", m_any_plugin_flag, [t_n5_installer_refuses])
    run_mutation(t, "the plugin command does not look for its file", m_no_fallback, [t_n5_command_in_git_bash])
    run_mutation(t, "the installer's timeout differs from the one in hooks.json", m_timeout_drift, [t_n5_files])


# =============================================================================================
# 15. proof instead of looks: the answer of the second code review -- a sign by looks is replaced by a PROOF
#     (each test here is red on the kit of order 5)
# =============================================================================================

def n6_store_env(sb):
    return env(SHEDKEEPER_STORE_ROOT=os.path.join(sb, "store"), SHEDKEEPER_INI=os.path.join(sb, "no.ini"))


def t_n6_record_proof(t):
    """Item 1. A dead line is taken out only when it is PROVEN to be the installer's own: its exact command text is in the
    record kept for THIS settings file. Lines that are not (a colleague's, an earlier release's, another file's) stay and are
    named; an unreadable record decides nothing and says so; a record that cannot be written is named at the install."""
    m = install_mod()
    sb = sandbox("n6r")
    sp = os.path.join(sb, "proj", ".claude", "settings.json")
    sp2 = os.path.join(sb, "proj2", ".claude", "settings.json")
    gone, gone2, gone3 = (os.path.join(sb, n) for n in ("old_place", "colleague_kit", "kit_0_1_0"))

    def line(kit, script="pre_tool.py", event="PreToolUse"):
        return m._build(os.path.join(kit, "hooks", script), event, sp)

    with n6_store_env(sb):
        rc, lines = m.install(sp)
        store_mod, st_settings, root = m._personal_root(sp)
        rec_file = os.path.join(root, "installs.jsonl")
        recs = [json.loads(x) for x in read(rec_file).splitlines()] if os.path.isfile(rec_file) else []
        cmds = all_commands(jload(sp))
        t.check("record: install writes one record per line it put in (3), in installs.jsonl of the personal folder, and says where",
                rc == 0 and len(recs) == 3 and any(x.startswith("install record: 3 new lines in ") for x in lines), (rc, lines, recs))
        t.check("record: each record holds the settings file, the EXACT command, the kit folder, the event, the script, the version, the time",
                sorted(r.get("command") for r in recs) == sorted(cmds) and all(
                    r.get("settings") == os.path.abspath(sp) and r.get("kit") == KIT and r.get("event") and r.get("script")
                    and r.get("version") == read(os.path.join(KIT, "VERSION")).strip() and r.get("time") for r in recs), recs[:1])
        rc, lines = m.install(sp)
        t.check("record: a second install adds no record (the lines are recorded once) and does not say it did",
                len(read(rec_file).splitlines()) == 3 and not any(x.startswith("install record") for x in lines), lines)
        t.check("record: recorded_commands(sp) is exactly the three commands; for another settings file it is empty",
                m.recorded_commands(sp)[0] == set(cmds) and m.recorded_commands(sp2)[0] == set(), m.recorded_commands(sp))
        # a line installed BEFORE the record existed is recorded by the next install (so that it can be taken out one day)
        write(rec_file, "")
        rc, lines = m.install(sp)
        t.check("record: lines of this kit that were put in before the record existed are recorded by the next install",
                len(read(rec_file).splitlines()) == 3 and any("3 new lines" in x for x in lines), lines)

        # --- recorded vs not recorded, the same form, the same dead folder
        rec_cmd, unrec_cmd, other_file_cmd = line(gone), line(gone, "session.py", "SessionStart"), line(gone2)
        old_plain = 'python "%s"' % os.path.join(gone3, "hooks", "pre_tool.py")
        n6_record(sp, [rec_cmd])
        n6_record(sp2, [other_file_cmd])                  # recorded for ANOTHER settings file: no proof for this one
        d = jload(sp)
        d["hooks"]["PreToolUse"].append({"hooks": [{"type": "command", "command": rec_cmd}]})
        d["hooks"]["PreToolUse"].append({"hooks": [{"type": "command", "command": other_file_cmd}]})
        d["hooks"]["PreToolUse"].append({"hooks": [{"type": "command", "command": old_plain}]})
        d["hooks"]["SessionStart"].append({"hooks": [{"type": "command", "command": unrec_cmd}]})
        write(sp, json.dumps(d, indent=2))
        rc, lines = m.install(sp)
        out = "\n".join(lines)
        after = all_commands(jload(sp))
        t.check("proof: the line the installer recorded and whose folder is gone is taken out and named",
                rc == 0 and rec_cmd not in after and "removed a line of a previous installation" in out
                and os.path.join(gone, "hooks", "pre_tool.py").replace("\\", "/") in out.replace("\\", "/"), out)
        t.check("proof: the SAME form, folder gone, NOT in the record (a colleague's line in a shared file, another tool's) stays and is named",
                unrec_cmd in after and "left a line in ShedKeeper's form that this installer did not write" in out, out)
        t.check("proof: a line recorded for ANOTHER settings file is no proof for this one: it stays",
                other_file_cmd in after, after)
        t.check("proof: an old-form line of 0.1.0 / 0.2.0 (never recorded) is not taken out by its looks", old_plain in after, after)
        rc2, lines2 = m.check(sp, os.path.join(sb, "proj"))
        out2 = "\n".join(lines2)
        t.check("check: the old-form line that is not recorded is named as what it is -- it BLOCKS, not written by this installer, remove by hand -- and fails",
                rc2 == 1 and "BLOCKS every guarded tool" in out2 and "did not write it" in out2 and "remove it by hand" in out2, out2[-700:])
        rc3, lines3 = m.uninstall(sp)
        out3 = "\n".join(lines3)
        after3 = all_commands(jload(sp))
        t.check("uninstall: takes out the entries of the kit, keeps (and names) the lines it did not write",
                rc3 == 0 and not any(m.is_ours(c) for c in after3) and unrec_cmd in after3 and old_plain in after3
                and other_file_cmd in after3 and "did not write" in out3, out3)

        # --- the record cannot be read: nothing is decided dead, and it is said
        sb3 = sandbox("n6r3")
        sp3 = os.path.join(sb3, "proj", ".claude", "settings.json")
        with env(SHEDKEEPER_STORE_ROOT=os.path.join(sb3, "store")):
            st3, set3, root3 = m._personal_root(sp3)
            t.check("record: the personal folder is made by the test the way the hooks make it", st3.ensure_store(root3, set3)[0])
            os.makedirs(os.path.join(root3, "installs.jsonl"))          # a folder where the record file should be: cannot be read
            cmd3 = m._build(os.path.join(sb3, "gone", "hooks", "pre_tool.py"), "PreToolUse", sp3)
            write(sp3, json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": cmd3}]}]}}))
            val, note = m.recorded_commands(sp3)
            t.check("record unreadable: recorded_commands says None and why (it is not an empty record)", val is None and "cannot be read" in note or "could not be read" in note, (val, note))
            rc, lines = m.install(sp3)
            out = "\n".join(lines)
            t.check("record unreadable: the install is done, the dead line stays, and both problems are SAID (no line taken out this time; the record of this install cannot be written)",
                    rc == 0 and cmd3 in all_commands(jload(sp3)) and "no line of an earlier installation is taken out this time" in out
                    and "the record of this installation could not be written" in out and "removed a line" not in out
                    and "PreToolUse/pre_tool.py: added" in out, out)
    # the warning at the hook now matches what the installer does (item 4)
    person, agent = m.lost_messages("C:/x/hooks/pre_tool.py", "C:/p/.claude/settings.json")
    t.check("the loud warning tells the person and the agent the truth about what the installer does: uninstall, or remove the lines; "
            "install from the new place takes out the lines it WROTE",
            "install.py uninstall" in person and "wrote itself" in person and "install.py uninstall" in agent and "delete by hand" in agent, (person, agent))


def t_n6_absence(t):
    """Item 1, the second half. 'Gone' is said only when the system answers 'not found'. A file that is there but cannot be looked at
    (another account's folder) is 'not known': the line stays and is named. A path without a drive letter is not looked for on the
    current drive."""
    m = install_mod()
    sb = sandbox("n6a")
    sp = os.path.join(sb, "proj", ".claude", "settings.json")
    kit = fake_kit(sb, "kit_here")
    here = os.path.join(kit, "hooks", "pre_tool.py")
    t.check("_absence: a file that is there -> present", m._absence(here) == "present")
    t.check("_absence: a missing file in a folder that is there -> absent", m._absence(os.path.join(kit, "hooks", "nope.py")) == "absent")
    t.check("_absence: a path under a FILE (NotADirectoryError) -> absent", m._absence(os.path.join(here, "x", "y.py")) == "absent")
    t.check("_absence: a missing folder -> absent", m._absence(os.path.join(sb, "no_such", "hooks", "pre_tool.py")) == "absent")
    letter = next((l for l in "ZYXWVUTSRQPONM" if not os.path.exists(l + ":\\")), None)
    if letter:
        t.check("_absence: a drive that cannot be reached -> offline (not absent)", m._absence(letter + ":\\k\\hooks\\pre_tool.py") == "offline")

    def line(path):
        return m._build(path, "PreToolUse", sp)

    with n6_store_env(sb):
        # 1. a refusal of access, simulated: os.stat of ONE path raises PermissionError
        target = os.path.join(sb, "alice_kit", "hooks", "pre_tool.py")
        write(target, "print()\n")
        cmd = line(target)
        n6_record(sp, [cmd])
        write(sp, json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": cmd}]}]}}))
        real_stat = os.stat

        def deny(p, *a, **k):
            if os.path.normcase(os.fspath(p)) == os.path.normcase(os.path.normpath(target)):
                raise PermissionError(13, "Access is denied")
            return real_stat(p, *a, **k)
        with patched(os, "stat", deny):
            st = m._absence(target)
            cd = m.classify_dead(cmd, {cmd})
            rc, lines = m.install(sp)
        out = "\n".join(lines)
        t.check("refused access (simulated): _absence says 'unknown', not 'absent'", st.startswith("unknown") and "PermissionError" in st, st)
        t.check("refused access (simulated): classify_dead says undecided, not dead", cd and cd[0] == "undecided" and "PermissionError" in cd[2], cd)
        t.check("refused access (simulated): install keeps the line of the other account and says why",
                rc == 0 and cmd in all_commands(jload(sp)) and "kept a line of another installation" in out
                and "could not be looked at (unknown: PermissionError)" in out, out)

        # 2. the same with the REAL permissions of Windows: a folder closed to this account
        sp4 = os.path.join(sb, "proj4", ".claude", "settings.json")
        locked = os.path.join(sb, "locked_kit")
        victim = write(os.path.join(locked, "hooks", "pre_tool.py"), "print()\n")
        cmd4 = m._build(victim, "PreToolUse", sp4)
        n6_record(sp4, [cmd4])
        write(sp4, json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": cmd4}]}]}}))
        user = os.environ.get("USERNAME")
        hooks_dir = os.path.join(locked, "hooks")
        try:
            try:
                r = subprocess.run(["icacls", hooks_dir, "/deny", "%s:(OI)(CI)(RD,REA,RA)" % user], capture_output=True)
                closed, why_not = bool(user) and r.returncode == 0, (r.stdout + r.stderr)[:120]
            except OSError as e:
                closed, why_not = False, str(e)
            if not closed:
                raise NotMeasured("icacls could not close a folder to this account: the real-permission case was NOT run (%s)" % why_not)
            exists_now = os.path.exists(victim)
            st = m._absence(victim)
            rc, lines = m.install(sp4)
            out = "\n".join(lines)
        finally:
            # the deny leaves READ_CONTROL and WRITE_DAC alone (denying them -- a plain (RX) does -- locks the folder against its
            # own owner in a folder that has an OWNER RIGHTS entry, which the temporary folder has: measured, order 6)
            back = subprocess.run(["icacls", hooks_dir, "/remove:d", user], capture_output=True)
            if back.returncode != 0:
                subprocess.run(["takeown", "/F", hooks_dir], capture_output=True)
                subprocess.run(["icacls", hooks_dir, "/remove:d", user], capture_output=True)
        t.check("real permissions: the file IS there, and os.path.exists says False all the same (the fault the rule is built on)",
                exists_now is False and os.path.isfile(victim), exists_now)
        t.check("real permissions: _absence says 'unknown', the line of the closed folder stays and is named",
                st.startswith("unknown") and cmd4 in all_commands(jload(sp4)) and "could not be looked at" in out, (st, out))
    # 3. a path without a drive letter is not looked for on the current drive
    posix = 'python "/c/Users/someone/kit/hooks/pre_tool.py"'
    t.check("a POSIX-style path (no drive): not judged at all -- even recorded", m.classify_dead(posix, {posix}) is None, m.classify_dead(posix, {posix}))
    t.check("classify_dead without a record (None): a gone folder is 'unrecorded' at most, never dead",
            (m.classify_dead(m._build(os.path.join(sb, "gone", "hooks", "pre_tool.py"), "PreToolUse", sp), None) or ("-",))[0] == "unrecorded")


def t_n6_plugin_proof(t):
    """Item 2 (a, b, c). 'The plugin is enabled' is a PROOF, not one line of a settings file: the files in the order of Claude Code
    (local > project > user; the first that says true / false decides), the plugin is listed in the registry, its folder is there with
    hooks.json and both hook files. Not proven -> install goes on and warns; proven -> install takes out its own dead lines, then refuses;
    check works on the INSTALLED copy."""
    m = install_mod()
    sb = sandbox("n6p")
    home = os.path.join(sb, "home")
    proj = os.path.join(sb, "proj")
    cl = os.path.join(proj, ".claude")
    os.makedirs(cl)
    os.makedirs(os.path.join(home, ".claude"))
    key = "shedkeeper@shedkeeper"
    loc, shared, user = (os.path.join(cl, "settings.local.json"), os.path.join(cl, "settings.json"),
                         os.path.join(home, ".claude", "settings.json"))

    def put(path, val):
        write(path, json.dumps({} if val is None else {"enabledPlugins": {key: val}}))

    def enabled():
        return [(os.path.basename(p), k) for p, k in m.plugin_enabled(m.applicable_settings(proj))]

    def run(*argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = m.main(list(argv))
        return rc, buf.getvalue()

    with n6_store_env(sb), env(SHEDKEEPER_USER_HOME=home, CLAUDE_PROJECT_DIR=None):
        # --- (a) the order of the files
        put(shared, True)
        t.check("order: true in the project file only -> enabled", enabled() == [("settings.json", key)], enabled())
        put(loc, False)
        t.check("order: false in settings.local.json beats true in settings.json -> NOT enabled", enabled() == [], enabled())
        put(shared, False)
        put(loc, True)
        t.check("order: true in settings.local.json beats false in settings.json -> enabled", enabled() == [("settings.local.json", key)], enabled())
        put(loc, None)
        put(shared, False)
        put(user, True)
        t.check("order: false in the project file beats true in the user file -> NOT enabled", enabled() == [], enabled())
        put(shared, None)
        t.check("order: true in the user file alone -> enabled", enabled() == [("settings.json", key)], enabled())
        write(user, json.dumps({"enabledPlugins": {key: "yes"}}))
        t.check("order: a value that is not true / false is not 'enabled'", enabled() == [], enabled())
        put(user, None)
        # --- (b) the proof, fault by fault
        put(shared, True)
        reg = os.path.join(home, ".claude", "plugins", "installed_plugins.json")

        def why():
            proven, unproven = m.plugin_status(proj)
            return proven, [w for _p, _k, ws in unproven for w in ws]

        proven, ws = why()
        t.check("proof: enabled, no plugin registry at all -> NOT proven, and the reason names the missing registry",
                not proven and any("does not exist" in w for w in ws), (proven, ws))
        write(reg, "{ not json")
        proven, ws = why()
        t.check("proof: the registry is unreadable -> NOT proven, and it says 'cannot be read' (an unreadable registry is not an empty one)",
                not proven and any("cannot be read" in w for w in ws), ws)
        write(reg, json.dumps({"plugins": []}))
        proven, ws = why()
        t.check("proof: the registry has no `plugins` object -> NOT proven (the format may have changed)", not proven and any("no `plugins` object" in w for w in ws), ws)
        write(reg, json.dumps({"version": 2, "plugins": {"other@x": []}}))
        proven, ws = why()
        t.check("proof: the registry does not list the key -> NOT proven", not proven and any("is not listed" in w for w in ws), ws)
        write(reg, json.dumps({"version": 2, "plugins": {key: [{"installPath": os.path.join(sb, "no_such_cache"), "version": "0.2.0"}]}}))
        proven, ws = why()
        t.check("proof: listed, but its folder is not there -> NOT proven", not proven and any("is not there" in w for w in ws), ws)
        half = os.path.join(sb, "half_cache")
        write(os.path.join(half, "hooks", "pre_tool.py"), "x\n")
        write(reg, json.dumps({"version": 2, "plugins": {key: [{"installPath": half, "version": "0.2.0"}]}}))
        proven, ws = why()
        t.check("proof: the folder is there but lacks hooks.json and session.py -> NOT proven and says what is missing",
                not proven and any("lacks" in w and "hooks/hooks.json" in w and "hooks/session.py" in w for w in ws), ws)
        cache = n6_plugin_installed(home, real=True)
        proven, ws = why()
        t.check("proof: enabled + listed + folder with hooks.json and both hook files -> proven, with the folder", [(k, d) for _p, k, d in proven] == [(key, cache)], (proven, ws))

        # --- (c) install
        write(reg, json.dumps({"version": 2, "plugins": {}}))              # written as enabled, NOT installed
        rc, out = run("install", "--project", proj)
        t.check("install: the plugin only WRITTEN as enabled -> install goes on (exit 0, entries added) and WARNS: written as enabled, not found installed, why",
                rc == 0 and "added" in out and "written as enabled" in out and "not found installed" in out and "is not listed" in out, (rc, out[:500]))
        m.uninstall(shared)
        cache = n6_plugin_installed(home, real=True)
        gone = os.path.join(sb, "old_kit")
        dead_cmd = m._build(os.path.join(gone, "hooks", "pre_tool.py"), "PreToolUse", shared)
        n6_record(shared, [dead_cmd])
        write(shared, json.dumps({"enabledPlugins": {key: True}, "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": dead_cmd}]}]}}, indent=2))
        rc, out = run("install", "--project", proj)
        after = jload(shared)
        t.check("install, plugin proven (F7): refused (exit 1) -- but the dead line of this installer is taken out FIRST and said, then REFUSED; no entry added",
                rc == 1 and "removed a line of a previous installation" in out and out.index("removed a line") < out.index("REFUSED")
                and "hooks" not in after and not any(m.is_ours(c) for c in all_commands(after)) and cache in out, (rc, out[:700], after))
        t.check("install, plugin proven: the refusal names what it saw (the key, the file, the installed folder)", key in out and shared in out, out[-500:])
        # --- check works on the INSTALLED copy
        write(shared, json.dumps({"enabledPlugins": {key: True}}))
        rc, out = run("check", "--project", proj)
        t.check("check, plugin: door = the plugin, the INSTALLED folder is named, hooks.json / plugin.json read from it, both probes OK, exit 0, and the "
                "probe says what it proves (starts and answers) and what it does not (a copy would be taken)",
                rc == 0 and "door: the plugin" in out and "installed plugin folder: %s" % cache in out and "hooks/hooks.json: the hooks of the plugin are the hooks" in out
                and out.count(": OK -- ") >= 1 and "probe of the fallback (the plugin root without the hook file): OK" in out and "starts and answers, not that a copy would be taken" in out, (rc, out[-900:]))
        good = read(os.path.join(cache, "hooks", "pre_tool.py"))
        write(os.path.join(cache, "hooks", "pre_tool.py"), "import sys\nsys.exit(7)\n")
        rc, out = run("check", "--project", proj)
        t.check("check, plugin: the hook of the INSTALLED copy is broken (this kit's own is fine) -> the probe FAILS and exit 1: the cache is probed, not the kit folder",
                rc == 1 and "root = the installed plugin folder" in out and "FAILED" in out, (rc, out[-500:]))
        write(os.path.join(cache, "hooks", "pre_tool.py"), good)
        hj = os.path.join(cache, "hooks", "hooks.json")
        write(hj, json.dumps({"hooks": {}}))
        rc, out = run("check", "--project", proj)
        t.check("check, plugin: the installed hooks.json differs from what the installer writes -> DIFFERS, exit 1", rc == 1 and "DIFFERS" in out, (rc, out[-400:]))
        write(hj, json.dumps(m.plugin_hooks(), indent=2))
        write(shared, json.dumps({"enabledPlugins": {key: True}, "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": dead_cmd}]}]}}))
        rc, out = run("check", "--project", proj)
        t.check("check, plugin (F7): the dead line of the settings is named (this installer's own) and fails the check",
                rc == 1 and "a line of a previous installation points to" in out and "install.py install" in out, (rc, out[-500:]))
        write(shared, json.dumps({"enabledPlugins": {key: True}}))
        write(loc, json.dumps({"disableAllHooks": True}))
        rc, out = run("check", "--project", proj)
        t.check("check, plugin: disableAllHooks in settings.local.json -> named with the file, exit 1 (the plugin door was green over it before)",
                rc == 1 and "disableAllHooks is TRUE in" in out and loc in out, (rc, out[-500:]))
        # written as enabled, not installed, and a live install.py door: the settings door is fine, the unproven plugin is NAMED
        write(loc, "{}")
        write(reg, json.dumps({"version": 2, "plugins": {}}))
        write(shared, json.dumps({"enabledPlugins": {key: True}}))
        rc, lines = m.install(shared)
        rc, out = run("check", "--project", proj)
        t.check("check: the plugin written as enabled but not installed + live entries of install.py -> door: install.py, and `plugin door NOT PROVEN` is printed",
                "door: install.py" in out and "plugin door NOT PROVEN" in out and "is not listed" in out, out[-700:])


def t_n6_yield_proof(t):
    """Item 2 (last). The plugin's hook yields only to a PROVEN live installation: Claude Code gave CLAUDE_PROJECT_DIR (the cwd of the
    event is not used), the entry is live, and disableAllHooks is on in no applicable file. Else it works (a doubled hook, never a silent pair)."""
    m = install_mod()
    pm = pre_tool
    sb = sandbox("n6y")
    home = os.path.join(sb, "home")
    with store_env(sb) as (root, st), env(SHEDKEEPER_USER_HOME=home, CLAUDE_PROJECT_DIR=None):
        os.makedirs(os.path.join(home, ".claude"))
        proj = n5_project(sb, "yp", "settings")
        ev, f = n5_pre_event(proj, sb, "rm")
        quiet = n5_run(pm, ev, True, project=proj)[1]
        t.check("yield: CLAUDE_PROJECT_DIR is given and the entry is live -> the plugin is silent (the control for the cases below)",
                quiet == "", quiet[:120])
        ev, f = n5_pre_event(proj, sb, "rm")
        t.check("yield: NO CLAUDE_PROJECT_DIR, the cwd of the event IS the project root with a live entry -> the plugin does NOT yield (the cwd proves nothing)",
                verdict(n5_run(pm, ev, True, project=None)[1]) == "deny")
        t.check("plugin_yields: without the variable it is [] whatever the cwd", m.plugin_yields("PreToolUse", proj) == [])
        with env(CLAUDE_PROJECT_DIR=proj):
            t.check("plugin_yields: with the variable, the live entries of that project's settings", m.plugin_yields("PreToolUse", None) != [])
        write(os.path.join(home, ".claude", "settings.json"), json.dumps({"disableAllHooks": True}))
        ev, f = n5_pre_event(proj, sb, "rm")
        t.check("yield: disableAllHooks is on in the USER file (the entries of install.py are not proven to run) -> the plugin works",
                verdict(n5_run(pm, ev, True, project=proj)[1]) == "deny")
        write(os.path.join(home, ".claude", "settings.json"), json.dumps({"disableAllHooks": False}))
        ev, f = n5_pre_event(proj, sb, "rm")
        t.check("yield: disableAllHooks false everywhere -> silent again", n5_run(pm, ev, True, project=proj)[1] == "")
        write(os.path.join(proj, ".claude", "settings.local.json"), json.dumps({"disableAllHooks": True}))
        ev, f = n5_pre_event(proj, sb, "rm")
        t.check("yield: disableAllHooks in settings.local.json -> the plugin works", verdict(n5_run(pm, ev, True, project=proj)[1]) == "deny")
        os.rename(os.path.join(proj, ".claude", "settings.local.json"), os.path.join(proj, ".claude", "settings.local.json.moved"))
        # a live entry whose hook file is gone is not proof
        dead = n5_project(sb, "yd", "dead")
        ev, f = n5_pre_event(dead, sb, "rm")
        t.check("yield: the entry names a kit folder that is gone -> the plugin works", verdict(n5_run(pm, ev, True, project=dead)[1]) == "deny")


def t_n6_config_dir(t):
    """Item 3. Where Claude Code is started with CLAUDE_CONFIG_DIR, the user settings and the plugin registry are THERE, not in ~/.claude.
    (USERPROFILE is pointed at an empty folder: whatever the kit does, the real ~/.claude of the person is not reached.)"""
    m = install_mod()
    sb = sandbox("n6c")
    cfg = os.path.join(sb, "cfg_dir")
    fake_home = os.path.join(sb, "fake_home")
    proj = os.path.join(sb, "proj")
    os.makedirs(fake_home)
    os.makedirs(proj)
    with env(SHEDKEEPER_USER_HOME=None, CLAUDE_CONFIG_DIR=cfg, USERPROFILE=fake_home, HOME=fake_home, CLAUDE_PROJECT_DIR=None,
             SHEDKEEPER_STORE_ROOT=os.path.join(sb, "store"), SHEDKEEPER_INI=os.path.join(sb, "no.ini")):
        t.check("config dir: user_claude_dir() is CLAUDE_CONFIG_DIR", m.user_claude_dir() == os.path.abspath(cfg), m.user_claude_dir())
        t.check("config dir: the user settings path is in it", m.settings_path(user=True) == os.path.join(os.path.abspath(cfg), "settings.json"),
                m.settings_path(user=True))
        t.check("config dir: the settings that apply to a project end with the user file of CLAUDE_CONFIG_DIR",
                m.applicable_settings(proj)[-1] == os.path.join(os.path.abspath(cfg), "settings.json"), m.applicable_settings(proj))
        rc, out = inst("install", "--user")
        t.check("config dir: `install --user` (a subprocess) writes the settings of CLAUDE_CONFIG_DIR and does not touch ~/.claude",
                rc == 0 and os.path.isfile(os.path.join(cfg, "settings.json")) and not os.path.exists(os.path.join(fake_home, ".claude")), (rc, out[:300]))
        rc, out = inst("check", "--user")
        t.check("config dir: `check --user` reads it, says where it reads from, and is green", rc == 0 and "CLAUDE_CONFIG_DIR: %s" % os.path.abspath(cfg) in out, (rc, out[:400]))
        # the plugin registry
        key = "shedkeeper@shedkeeper"
        n6_plugin_installed(None, key, claude_dir=cfg)
        write(os.path.join(proj, ".claude", "settings.local.json"), json.dumps({"enabledPlugins": {key: True}}))
        proven, unproven = m.plugin_status(proj)
        t.check("config dir: the plugin registry is read from CLAUDE_CONFIG_DIR/plugins -> the plugin is proven there", [k for _p, k, _d in proven] == [key], (proven, unproven))
        # the plugin yields to an entry in the user settings of CLAUDE_CONFIG_DIR (and not to one in ~/.claude)
        with env(CLAUDE_PROJECT_DIR=proj):
            t.check("config dir: a live entry in the user settings of CLAUDE_CONFIG_DIR covers the event for the plugin", m.plugin_yields("PreToolUse") != [])
        # SHEDKEEPER_USER_HOME (the explicit override) wins over the variable
        other = os.path.join(sb, "explicit_home")
        with env(SHEDKEEPER_USER_HOME=other):
            t.check("config dir: SHEDKEEPER_USER_HOME, set, names the home and wins over CLAUDE_CONFIG_DIR", m.user_claude_dir() == os.path.join(other, ".claude"))
    with env(SHEDKEEPER_USER_HOME=None, CLAUDE_CONFIG_DIR=None, USERPROFILE=fake_home):
        t.check("config dir: no variable -> ~/.claude of the user", m.user_claude_dir() == os.path.join(os.path.expanduser("~"), ".claude"), m.user_claude_dir())


def n7_world(label):
    """A sandbox the way n6_config_dir has it: no override variable, USERPROFILE at an empty folder. -> (sb, fake_home, proj, key)."""
    sb = sandbox(label)
    fake_home = os.path.join(sb, "fake_home")
    proj = os.path.join(sb, "proj")
    os.makedirs(fake_home)
    os.makedirs(proj)
    return sb, fake_home, proj, "shedkeeper@shedkeeper"


def n7_env(sb, fake_home, **over):
    kw = dict(SHEDKEEPER_USER_HOME=None, CLAUDE_CONFIG_DIR=None, CLAUDE_CODE_PLUGIN_CACHE_DIR=None, USERPROFILE=fake_home, HOME=fake_home,
              CLAUDE_PROJECT_DIR=None, SHEDKEEPER_STORE_ROOT=os.path.join(sb, "store"), SHEDKEEPER_INI=os.path.join(sb, "no.ini"))
    kw.update(over)
    return env(**kw)


def t_n7_plugins_root(t):
    """F1. CLAUDE_CODE_PLUGIN_CACHE_DIR moves the plugin registry (and the cache): the registry is read THERE, not in ~/.claude/plugins."""
    m = install_mod()
    sb, fake_home, proj, key = n7_world("n7r")
    pr = os.path.join(sb, "plugins_root")
    default_pl = os.path.join(fake_home, ".claude", "plugins")
    write(os.path.join(proj, ".claude", "settings.local.json"), json.dumps({"enabledPlugins": {key: True}}))
    with n7_env(sb, fake_home, CLAUDE_CODE_PLUGIN_CACHE_DIR=pr):
        t.check("plugins root: the variable (an absolute path) is the root", m.plugins_root() == os.path.normpath(pr), m.plugins_root())
        # the registry with the plugin lies only in the folder of the variable
        cache = n6_plugin_installed(None, key, claude_dir=os.path.join(fake_home, ".claude"), plugins_root=pr, real=True)
        proven, unproven = m.plugin_status(proj)
        t.check("plugins root: the registry in CLAUDE_CODE_PLUGIN_CACHE_DIR lists the plugin -> the plugin door is PROVEN", [k for _p, k, _d in proven] == [key], (proven, unproven))
        rc, out = inst("install", "--project", proj)
        t.check("plugins root: `install` is REFUSED while the plugin is proven there (exit 1, no entry added)", rc == 1 and "REFUSED" in out and cache in out, (rc, out[:400]))
        rc, out = inst("check", "--project", proj)
        t.check("plugins root: `check` says where it reads the registry from and that the door is the plugin",
                "the plugin registry is read from CLAUDE_CODE_PLUGIN_CACHE_DIR: %s" % os.path.normpath(pr) in out and "door: the plugin" in out, (rc, out[:500]))
    # the registry only in ~/.claude/plugins while the variable points elsewhere -> NOT proven
    other = os.path.join(sb, "elsewhere")
    with n7_env(sb, fake_home):
        n6_plugin_installed(None, key, claude_dir=os.path.join(fake_home, ".claude"), real=True)
    with n7_env(sb, fake_home, CLAUDE_CODE_PLUGIN_CACHE_DIR=other):
        proven, unproven = m.plugin_status(proj)
        why = [w for _p, _k, ws in unproven for w in ws]
        t.check("plugins root: the variable is set and the plugin is listed only in ~/.claude/plugins -> NOT proven, the reason names the registry of the variable",
                not proven and any(os.path.join(os.path.normpath(other), "installed_plugins.json") in w for w in why), (proven, why))
        rc, out = inst("check", "--project", proj)
        t.check("plugins root: ... and `check` says `plugin door NOT PROVEN` and where it looked",
                "plugin door NOT PROVEN" in out and "CLAUDE_CODE_PLUGIN_CACHE_DIR: %s" % os.path.normpath(other) in out, (rc, out[:600]))
    with n7_env(sb, fake_home):
        t.check("plugins root: no variable -> ~/.claude/plugins", m.plugins_root() == default_pl, m.plugins_root())
    cfg = os.path.join(sb, "cfg")
    with n7_env(sb, fake_home, CLAUDE_CONFIG_DIR=cfg):
        t.check("plugins root: CLAUDE_CONFIG_DIR alone -> plugins under it", m.plugins_root() == os.path.join(os.path.abspath(cfg), "plugins"), m.plugins_root())
    with n7_env(sb, fake_home, CLAUDE_CODE_PLUGIN_CACHE_DIR=pr, SHEDKEEPER_USER_HOME=os.path.join(sb, "explicit")):
        t.check("plugins root: SHEDKEEPER_USER_HOME (the explicit override) wins over the variable", m.plugins_root() == os.path.join(sb, "explicit", ".claude", "plugins"), m.plugins_root())


def t_n7_run_folder(t):
    """F2. A marketplace added from a folder (source `directory`) runs the plugin from THAT folder; the registry's installPath is a copy that does
    not run. Not determined -> not proven. Any other source -> the cache copy."""
    m = install_mod()
    sb, fake_home, proj, key = n7_world("n7f")
    d = os.path.join(sb, "marketfolder")
    km = os.path.join(fake_home, ".claude", "plugins", "known_marketplaces.json")
    write(os.path.join(proj, ".claude", "settings.local.json"), json.dumps({"enabledPlugins": {key: True}}))
    with n7_env(sb, fake_home):
        cache = n6_plugin_installed(None, key, claude_dir=os.path.join(fake_home, ".claude"), real=True, directory=d)
        t.check("run folder: source `directory` -> the folder it runs from is that folder (not installPath)", m.plugin_run_folder(key) == (os.path.normpath(d), None), m.plugin_run_folder(key))
        proven, unproven = m.plugin_status(proj)
        t.check("run folder: ... and the proof names the run folder", [(k, dd) for _p, k, dd in proven] == [(key, os.path.normpath(d))], (proven, unproven))
        rc, out = inst("check", "--project", proj)
        t.check("run folder: `check` runs the probe on that folder, names both folders and says the cache copy is not what runs; exit 0",
                rc == 0 and "installed plugin folder: %s" % os.path.normpath(d) in out and "the plugin runs from %s" % os.path.normpath(d) in out
                and "installPath is another folder, %s" % cache in out and "is NOT what runs" in out and "probe (the plugin's pre_tool command" in out, (rc, out[:900]))
        rc, out = inst("install", "--project", proj)
        t.check("run folder: `install` refuses and names the run folder", rc == 1 and "REFUSED" in out and "installed at %s" % os.path.normpath(d) in out, (rc, out[:400]))
        # the cache copy is intact, the folder it runs from is not -> NOT proven (the cache is not tried for source `directory`)
        empty = os.path.join(sb, "emptyfolder")
        os.makedirs(empty)
        write(km, json.dumps({"shedkeeper": {"source": {"source": "directory", "path": empty}, "installLocation": empty}}))
        proven, unproven = m.plugin_status(proj)
        why = [w for _p, _k, ws in unproven for w in ws]
        t.check("run folder: source `directory`, its folder lacks the hook files while the cache copy is intact -> NOT proven, the reason names the run folder",
                not proven and any(os.path.normpath(empty) in w and "lacks" in w for w in why), (proven, why))
        # not determined: unreadable / without the marketplace / without a source / without a path / not an object
        for label, content in (("is unreadable", "{ not json"),
                               ("lacks the marketplace", json.dumps({"other": {"source": {"source": "directory", "path": d}}})),
                               ("has no source", json.dumps({"shedkeeper": {"installLocation": d}})),
                               ("has `directory` without a path", json.dumps({"shedkeeper": {"source": {"source": "directory"}}})),
                               ("is not an object", json.dumps([1, 2]))):
            write(km, content)
            run, note = m.plugin_run_folder(key)
            proven, unproven = m.plugin_status(proj)
            why = [w for _p, _k, ws in unproven for w in ws]
            t.check("run folder: known_marketplaces.json %s -> not determined (None + a note), NOT proven; the reason says the cache copy is intact but not shown to run" % label,
                    run is None and bool(note) and not proven and any("not determined" in w and "intact" in w for w in why), (run, note, proven, why))
        rc, out = inst("check", "--project", proj)
        t.check("run folder: not determined -> `check` says so and does not call the door proven", "not determined" in out and "plugin door NOT PROVEN" in out, (rc, out[:500]))
        os.remove(km)         # a file of this test's own sandbox
        run, note = m.plugin_run_folder(key)
        t.check("run folder: no known_marketplaces.json -> not determined, and the note says it does not exist", run is None and "does not exist" in (note or ""), (run, note))
        # another source: the cache copy runs
        for src in ({"source": "github", "repo": "x/y"}, {"source": "git", "url": "https://example.invalid/x.git"}):
            write(km, json.dumps({"shedkeeper": {"source": src}}))
            proven, _u = m.plugin_status(proj)
            t.check("run folder: source `%s` -> the cache copy runs and is proven" % src["source"],
                    m.plugin_run_folder(key) == ("", None) and [dd for _p, _k, dd in proven] == [cache], (m.plugin_run_folder(key), proven))


def t_n7_subfolder_note(t):
    """F3. `install --project` says in one line that a session started in a subfolder is not covered (in-process, so that the mutation reaches it;
    and once as a subprocess, the way a person runs it)."""
    m = install_mod()
    sb, fake_home, proj, key = n7_world("n7s")

    def run(argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = m.main(argv)
        return rc, buf.getvalue()

    with n7_env(sb, fake_home):
        rc, out = run(["install", "--project", proj])
        notes = [l for l in out.splitlines() if "subfolder" in l]
        t.check("install --project: exactly one line says a session started in a subfolder is not protected and --user is for those",
                rc == 0 and len(notes) == 1 and "--user" in notes[0] and "STARTED" in notes[0], (rc, out[-500:]))
        proj2 = os.path.join(sb, "proj2")
        os.makedirs(proj2)
        rc, out = inst("install", "--project", proj2)
        t.check("install --project (a subprocess): the same line", rc == 0 and out.count("subfolder") == 1, (rc, out[-400:]))
        rc, out = run(["install", "--user"])
        t.check("install --user: no such line (it covers every folder)", rc == 0 and "subfolder" not in out, (rc, out[-300:]))
        rc, out = run(["install", "--project", proj])
        t.check("install --project, nothing to change (a second run): still says it once", rc == 0 and out.count("subfolder") == 1, (rc, out[-300:]))


def t_n8_plugins_root(t):
    """The third review round. A relative CLAUDE_CODE_PLUGIN_CACHE_DIR is NOT taken for a root and NOT ignored: where Claude Code
    reads from is not determined, so the plugin is not proven (the registry of the default folder is not read). `~` is expanded. Both `check` and
    `install` always say where the registry is read from, and that it is the environment of THIS process."""
    m = install_mod()
    sb, fake_home, proj, key = n7_world("n8r")
    default_pl = os.path.join(fake_home, ".claude", "plugins")
    write(os.path.join(proj, ".claude", "settings.local.json"), json.dumps({"enabledPlugins": {key: True}}))
    with n7_env(sb, fake_home):
        n6_plugin_installed(None, key, claude_dir=os.path.join(fake_home, ".claude"), real=True)       # an intact plugin in the DEFAULT root
    with n7_env(sb, fake_home, CLAUDE_CODE_PLUGIN_CACHE_DIR="relative/plugins"):
        proven, unproven = m.plugin_status(proj)
        why = [w for _p, _k, ws in unproven for w in ws]
        t.check("relative variable: the plugin of the default registry is NOT proven, the reason says the root is not determined",
                not proven and any("not an absolute path" in w and "not determined" in w for w in why), (proven, why))
        t.check("relative variable: plugins_root_problem() is set; the lines say it, and that the default folder was NOT read",
                bool(m.plugins_root_problem()) and any("not determined" in l and "NOT read" in l for l in m.plugins_root_lines()), m.plugins_root_lines())
        rc, out = inst("install", "--project", proj)
        t.check("relative variable: `install` is not REFUSED (no proof) and says why", rc == 0 and "REFUSED" not in out and "not an absolute path" in out, (rc, out[:500]))
        rc, out = inst("check", "--project", proj)
        t.check("relative variable: `check` says `plugin door NOT PROVEN` and that the root is not determined", "plugin door NOT PROVEN" in out and "not determined" in out, (rc, out[:600]))
    # `~` is expanded: the plugin registry in <home>/pl is read
    pl = os.path.join(fake_home, "pl")
    with n7_env(sb, fake_home):
        n6_plugin_installed(None, key, plugins_root=pl, claude_dir=os.path.join(fake_home, ".claude"), real=True)
    with n7_env(sb, fake_home, CLAUDE_CODE_PLUGIN_CACHE_DIR="~/pl"):
        t.check("`~/pl`: expanded to the home folder, a root", m.plugins_root() == os.path.normpath(pl) and m.plugins_root_problem() is None, (m.plugins_root(), m.plugins_root_problem()))
        proven, unproven = m.plugin_status(proj)
        t.check("`~/pl`: the plugin of that registry is proven", [k for _p, k, _d in proven] == [key], (proven, unproven))
    # the line is always there, and says whose environment it is
    with n7_env(sb, fake_home):
        lines = m.plugins_root_lines()
        t.check("no variable: the line names the folder, says the variable is not set in THIS process and that Claude Code reads its own environment",
                len(lines) == 1 and default_pl in lines[0] and "not set in the environment of this process" in lines[0] and "reads its own environment" in lines[0], lines)
        for action in ("install", "check"):
            rc, out = inst(action, "--project", proj)
            t.check("no variable: `%s --project` prints that line" % action, "the plugin registry is read from %s (CLAUDE_CODE_PLUGIN_CACHE_DIR is not set" % default_pl in out, (rc, out[:500]))
    with n7_env(sb, fake_home, CLAUDE_CODE_PLUGIN_CACHE_DIR=os.path.join(sb, "abs_root")):
        rc, out = inst("install", "--project", proj)
        t.check("variable set: `install` prints the root it reads, and that it is this process's environment",
                "read from CLAUDE_CODE_PLUGIN_CACHE_DIR: %s (the environment of this process" % os.path.normpath(os.path.join(sb, "abs_root")) in out, (rc, out[:500]))


def t_n8_run_folder(t):
    """The third review round. Only `github` and `git` are taken to run from the cache copy; an unknown source kind and a
    relative `directory` path are NOT determined, so the plugin is not proven (install.py then puts its own door)."""
    m = install_mod()
    sb, fake_home, proj, key = n7_world("n8f")
    km = os.path.join(fake_home, ".claude", "plugins", "known_marketplaces.json")
    write(os.path.join(proj, ".claude", "settings.local.json"), json.dumps({"enabledPlugins": {key: True}}))
    with n7_env(sb, fake_home):
        n6_plugin_installed(None, key, claude_dir=os.path.join(fake_home, ".claude"), real=True)       # an intact copy in the cache
        for src in ({"source": "somethingnew", "x": 1}, {"source": "url", "url": "https://example.invalid/m.json"}, {"source": "npm"}, {"source": "file", "path": sb}):
            write(km, json.dumps({"shedkeeper": {"source": src}}))
            run, note = m.plugin_run_folder(key)
            proven, unproven = m.plugin_status(proj)
            why = [w for _p, _k, ws in unproven for w in ws]
            t.check("source kind `%s`: not studied -> not determined (None + a note naming the kind), NOT proven although the cache copy is intact" % src["source"],
                    run is None and src["source"] in (note or "") and "not been studied" in note and not proven and any("not determined" in w for w in why), (run, note, proven, why))
        rc, out = inst("install", "--project", proj)
        t.check("an unknown source kind: `install` is not REFUSED (it puts its own door in) and warns that the plugin is not counted",
                rc == 0 and "REFUSED" not in out and "not counted" in out, (rc, out[:500]))
        for src in ({"source": "github", "repo": "x/y"}, {"source": "git", "url": "https://example.invalid/x.git"}):
            write(km, json.dumps({"shedkeeper": {"source": src}}))
            t.check("source kind `%s`: still the cache copy" % src["source"], m.plugin_run_folder(key) == ("", None), m.plugin_run_folder(key))
        for rel in ("kit", "./kit", "..", "."):
            write(km, json.dumps({"shedkeeper": {"source": {"source": "directory", "path": rel}}}))
            run, note = m.plugin_run_folder(key)
            proven, _u = m.plugin_status(proj)
            t.check("source `directory`, path %r (relative): not determined, NOT proven (it would be read against the cwd of this process)" % rel,
                    run is None and "not absolute" in (note or "") and not proven, (run, note, proven))
        write(km, json.dumps({"shedkeeper": {"source": {"source": ["directory"]}}}))
        t.check("a `source.source` that is not a string: not determined", m.plugin_run_folder(key)[0] is None, m.plugin_run_folder(key))
    t.check("docstring of plugin_run_folder: valid for a plugin with source `./` (the marketplace folder is taken for the plugin folder)",
            "./" in m.plugin_run_folder.__doc__ and "subfolder" in m.plugin_run_folder.__doc__, m.plugin_run_folder.__doc__)


def t_n8_check_shapes(t):
    """The third review round. A plugin.json / hooks.json of the run folder that holds not an object: a line and a red result, not a traceback."""
    sb, fake_home, proj, key = n7_world("n8s")
    d = os.path.join(sb, "marketfolder")
    write(os.path.join(proj, ".claude", "settings.local.json"), json.dumps({"enabledPlugins": {key: True}}))
    with n7_env(sb, fake_home):
        n6_plugin_installed(None, key, claude_dir=os.path.join(fake_home, ".claude"), real=True, directory=d)
        for rel in (".claude-plugin/plugin.json", "hooks/hooks.json"):
            for content in ('[1, 2]', '"text"', 'null'):
                keep = read(os.path.join(d, *rel.split("/")))
                write(os.path.join(d, *rel.split("/")), content)
                rc, out = inst("check", "--project", proj)
                t.check("%s holds %s: `check` says it is not understood, exit 1, no traceback" % (rel, content),
                        rc == 1 and "%s: not understood" % rel in out and "Traceback" not in out, (rc, out[:700]))
                write(os.path.join(d, *rel.split("/")), keep)


def t_n8_subfolder_check(t):
    """The third review round. `check --project` repeats the subfolder limit when the door that covers the session is in the settings of THAT
    project (an entry of install.py; a plugin at the project or local scope); not for the user settings, not without --project."""
    sb, fake_home, proj, key = n7_world("n8n")
    d = os.path.join(sb, "marketfolder")
    with n7_env(sb, fake_home):
        rc, out = inst("install", "--project", proj)
        rc, out = inst("check", "--project", proj)
        t.check("check --project, the door is install.py in the project settings: the subfolder NOTE", rc == 0 and out.count("a session started in a subfolder") == 1 and "NOTE:" in out, (rc, out[-500:]))
        rc, out = inst("uninstall", "--project", proj)
        n6_plugin_installed(None, key, claude_dir=os.path.join(fake_home, ".claude"), real=True, directory=d)
        write(os.path.join(proj, ".claude", "settings.local.json"), json.dumps({"enabledPlugins": {key: True}}))
        rc, out = inst("check", "--project", proj)
        t.check("check --project, the door is the plugin at the local scope: the subfolder NOTE", "door: the plugin" in out and out.count("a session started in a subfolder") == 1, (rc, out[-600:]))
        write(os.path.join(proj, ".claude", "settings.local.json"), "{}")
        write(os.path.join(fake_home, ".claude", "settings.json"), json.dumps({"enabledPlugins": {key: True}}))
        rc, out = inst("check", "--project", proj)
        t.check("check --project, the plugin is enabled at the USER scope only: no NOTE (the user settings are read in every folder)", "door: the plugin" in out and "subfolder" not in out, (rc, out[-600:]))
        os.remove(os.path.join(fake_home, ".claude", "settings.json"))        # a file of this test's own sandbox
        rc, out = inst("install", "--user")
        rc, out = inst("check", "--project", proj)
        t.check("check --project, the entries of install.py are in the USER settings only: no NOTE", "subfolder" not in out, (rc, out[-500:]))
        rc, out = inst("check", "--user")
        t.check("check --user: no NOTE", "subfolder" not in out, (rc, out[-500:]))


def t_n8_docs(t):
    """README says what was measured and what is only expected."""
    readme = " ".join(read(README).split())
    t.check("README: the GitHub/git case is 'expected from the documentation, not measured', not 'measured'",
            "expected from the documentation, not measured here" in readme and "(measured on Claude Code 2.1.284, Windows). A marketplace from GitHub or git" not in readme, "missing")
    t.check("README: only github and git are taken to run from the cache; any other source kind is 'not determined'",
            "any other kind of source" in readme and "not determined" in readme, "missing")
    t.check("README: the plugin at the user scope covering subfolders is 'expected, not measured'", "expected" in readme and "not measured" in readme and "the plugin at the user scope" in readme, "missing")
    t.check("README: the registry is read from the environment of the process that runs install.py / check, and Claude Code reads its own",
            "environment of the process that runs `install.py`" in readme, "missing")
    t.check("README: a relative CLAUDE_CODE_PLUGIN_CACHE_DIR is not determined (not ignored, not guessed)", "relative `CLAUDE_CODE_PLUGIN_CACHE_DIR`" in readme, "missing")
    t.check("README: the folder of a folder-marketplace is the plugin folder only for a plugin with `\"source\": \"./\"`", '"source": "./"' in readme, "missing")


def t_n7_docs(t):
    """F2/F3 and the stand: the README says what was measured, and does not say what the stand refuted."""
    readme = " ".join(read(README).split())
    t.check("README: the false claim -- a subfolder session is covered by the entries -- is gone", "A session started in a subfolder, `claude --plugin-dir`" not in readme, "present")
    t.check("README: the measured subfolder fact -- the project settings are read from the .claude of the folder Claude Code is started in; --project does not cover a subfolder; --user does",
            "reads the project settings only from the `.claude` of the folder it is started in" in readme and "does not protect a session started in a subfolder" in readme
            and "`--user`" in readme, "missing")
    t.check("README: a marketplace folder runs in place (the cache copy is not what runs); do not move or delete it; shedkeeper.ini in it is the one in force",
            "runs from that folder itself" in readme and "do not move or delete that folder" in readme and "`shedkeeper.ini` in it is the one in force" in readme, "missing")
    t.check("README: the claim that Claude Code never uses the plugin folder in place is gone", "does not use the plugin folder in place" not in readme, "present")
    t.check("README: CLAUDE_CODE_PLUGIN_CACHE_DIR is named", "CLAUDE_CODE_PLUGIN_CACHE_DIR" in readme, "missing")
    t.check("README: the 'not run inside a live Claude Code session' paragraph is replaced by the stand result (date, version) and names what is NOT checked",
            "was not run inside a live Claude Code session" not in readme and "2026-10-01" in readme and "2.1.284" in readme
            and "CLAUDE_CODE_PLUGIN_SEED_DIR" in readme and "GitHub marketplace" in readme and "exFAT" in readme and "second user" in readme, "missing")


def t_mutations_n7(t):
    """the stand findings: a guarded place is broken in memory and the matching test must turn red."""
    m = install_mod()

    def m_root_always_default():         # the registry is always read from ~/.claude/plugins
        return patched(m, "plugins_root", lambda: os.path.join(m.user_claude_dir(), "plugins"))

    def m_cache_for_directory():         # the cache copy is tried even when the marketplace is a folder
        return patched(m, "plugin_run_folder", lambda key: ("", None))

    def m_undetermined_is_cache():       # "not determined" is taken for "the cache copy runs"
        real = m.plugin_run_folder

        def run(key):
            r = real(key)
            return ("", None) if r[0] is None else r
        return patched(m, "plugin_run_folder", run)

    def m_no_subfolder_note():           # install --project does not say it
        return patched(m, "SUBFOLDER_NOTE", "")

    run_mutation(t, "the registry is always read from ~/.claude/plugins", m_root_always_default, [t_n7_plugins_root])
    run_mutation(t, "the cache copy is tried although the marketplace is a folder (source directory)", m_cache_for_directory, [t_n7_run_folder])
    run_mutation(t, "a run folder that is not determined is taken for the cache copy", m_undetermined_is_cache, [t_n7_run_folder])
    run_mutation(t, "install --project does not say that a subfolder is not covered", m_no_subfolder_note, [t_n7_subfolder_note])


def t_mutations_n8(t):
    """the second plugin review: a guarded place is broken in memory and the matching test must turn red."""
    m = install_mod()

    def m_unknown_kind_is_cache():       # a source kind not studied is taken for the cache copy
        real = m.plugin_run_folder

        def run(key):
            r = real(key)
            return ("", None) if r[0] is None and "has not been studied" in (r[1] or "") else r
        return patched(m, "plugin_run_folder", run)

    def m_relative_path_accepted():      # a relative `directory` path is taken as it stands
        real = m.plugin_run_folder

        def run(key):
            r = real(key)
            if r[0] is None and "not absolute" in (r[1] or ""):
                data, _why = m._read_plugin_json("known_marketplaces.json")
                return os.path.normpath(data[key.split("@", 1)[1]]["source"]["path"]), None
            return r
        return patched(m, "plugin_run_folder", run)

    def m_relative_var_accepted():       # a relative variable is taken for the default root
        return patched(m, "plugins_root_problem", lambda: None)

    def m_no_root_line_without_variable():       # the line about the root is printed only when the variable moved it
        real = m.plugins_root_lines
        return patched(m, "plugins_root_lines", lambda: real() if os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR") else [])

    run_mutation(t, "an unknown source kind is taken for the cache copy", m_unknown_kind_is_cache, [t_n8_run_folder])
    run_mutation(t, "a relative source.path is accepted", m_relative_path_accepted, [t_n8_run_folder])
    run_mutation(t, "a relative CLAUDE_CODE_PLUGIN_CACHE_DIR is taken for the default root", m_relative_var_accepted, [t_n8_plugins_root])
    run_mutation(t, "the line about the registry root is not printed without the variable", m_no_root_line_without_variable, [t_n8_plugins_root])


def t_n9_clone_copy(t):
    """K1: the tests copy the kit WITHOUT `.git` -- a clone of the repository has one, with read-only objects."""
    sb = sandbox("n9k")
    fake = os.path.join(sb, "clone")
    write(os.path.join(fake, "shedkeeper", "x.py"), "x = 1\n")
    write(os.path.join(fake, "hooks", "h.py"), "h = 1\n")
    write(os.path.join(fake, "tests", "t.py"), "t = 1\n")
    obj = write(os.path.join(fake, ".git", "objects", "ab", "cdef"), "blob\n")
    os.chmod(obj, 0o444)                                             # git keeps its objects read-only
    home = os.path.join(sb, "home")
    me = sys.modules[__name__]
    errors = []
    with patched(me, "KIT", fake):
        for round_no in (1, 2):                                      # the second copy goes over the first: where read-only files bite
            try:
                root = n6_plugin_installed(home, real=True)
            except Exception as e:
                errors.append("round %d: %s: %s" % (round_no, e.__class__.__name__, e))
                root = os.path.join(home, ".claude", "plugins", "cache", "shedkeeper", "shedkeeper", "0.2.0")
    t.check("clone copy: a kit with a `.git` (read-only objects) is copied twice without an error", not errors, errors)
    t.check("clone copy: the copy holds the kit's files, and no `.git`, no tests",
            os.path.isfile(os.path.join(root, "shedkeeper", "x.py")) and os.path.isfile(os.path.join(root, "hooks", "h.py"))
            and not os.path.exists(os.path.join(root, ".git")) and not os.path.exists(os.path.join(root, "tests")), listing(root))


def _n9_flaky(real, failures, calls):
    """A replacement for os.replace / os.rename: PermissionError(13) for the first `failures` calls on a temporary
    label name (None -> always), then the real one. Other names go to the real one at once."""
    def fake(src, dst, *a, **k):
        if ".part-" in os.path.basename(str(src)):
            calls.append(src)
            if failures is None or len(calls) <= failures:
                raise PermissionError(13, "Access is denied")
        return real(src, dst, *a, **k)
    return fake


def t_n9_label_retry(t):
    """K2: a label rename that meets another process replacing the same file (PermissionError) is tried again, within a
    bounded time; after the last try the refusal stands, with the reason; no temporary file of ours is left."""
    sb = sandbox("n9l")
    sleeps = []
    keep_sleep = lambda s: sleeps.append(s)         # noqa: E731 -- the pauses are recorded, not slept

    def parts(d):
        return [n for n in listing(d) if ".part-" in n]

    def attempt(fn):
        try:
            fn()
            return None
        except Exception as e:
            return "%s: %s" % (e.__class__.__name__, e)

    # --- the label: fails 3 times, then goes through
    d1 = os.path.join(sb, "d1")
    os.makedirs(d1)
    calls = []
    with patched(os, "replace", _n9_flaky(os.replace, 3, calls)), patched(store.time, "sleep", keep_sleep):
        err = attempt(lambda: store._write_json_label(d1, ".shedkeeper-store", {"product": config.PRODUCT}))
    t.check("label retry: 3 PermissionErrors then success -> no error, the label is in place, tried 4 times",
            err is None and len(calls) == 4 and os.path.isfile(os.path.join(d1, ".shedkeeper-store")), (err, len(calls), listing(d1)))
    t.check("label retry: ... and no temporary file is left", not parts(d1), listing(d1))
    # --- always: the old refusal, with the reason, after a bounded number of tries and bounded pauses
    d2 = os.path.join(sb, "d2")
    os.makedirs(d2)
    calls = []
    del sleeps[:]
    with patched(os, "replace", _n9_flaky(os.replace, None, calls)), patched(store.time, "sleep", keep_sleep):
        err = attempt(lambda: store._write_json_label(d2, ".shedkeeper-store", {"product": config.PRODUCT}))
    t.check("label retry (control): always PermissionError -> refused with the reason after exactly PLACE_TRIES tries",
            err is not None and "PermissionError" in err and "Access is denied" in err and len(calls) == store.PLACE_TRIES, (err, len(calls)))
    t.check("label retry (control): ... no label, and no temporary file of ours left behind",
            not os.path.exists(os.path.join(d2, ".shedkeeper-store")) and not parts(d2), listing(d2))
    t.check("label retry: the pauses in total stay far below the hook timeout (<= 2 s of 60 s)", sum(sleeps) <= 2.0 and len(sleeps) == store.PLACE_TRIES - 1,
            (sum(sleeps), len(sleeps)))
    # --- another error is not a collision: refused at once
    d3 = os.path.join(sb, "d3")
    os.makedirs(d3)
    calls = []

    def broken(src, dst, *a, **k):
        calls.append(src)
        raise OSError(errno.EIO, "disk error")
    with patched(os, "replace", broken), patched(store.time, "sleep", keep_sleep):
        err = attempt(lambda: store._write_json_label(d3, ".shedkeeper-store", {"product": config.PRODUCT}))
    t.check("label retry: an error that is not PermissionError is refused at once (1 try), temporary file removed",
            err is not None and "disk error" in err and len(calls) == 1 and not parts(d3), (err, len(calls), listing(d3)))
    # --- the whole store on an empty volume: the first two label renames collide, the store is usable
    vol = os.path.join(sb, "vol")
    pers = os.path.join(vol, real_login())
    calls = []
    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=os.path.join(sb, "no.ini")), \
            patched(os, "replace", _n9_flaky(os.replace, 2, calls)), patched(store.time, "sleep", keep_sleep):
        ok, why = store.ensure_store(pers, settings())
    t.check("label retry: a store on an empty volume whose first label renames collide -> usable, no refusal, nothing left over",
            ok and len(calls) >= 3 and os.path.isdir(pers) and not parts(vol) and not parts(pers), (ok, why, len(calls), listing(vol)))
    vol2 = os.path.join(sb, "vol2")
    with env(SHEDKEEPER_STORE_ROOT=vol2, SHEDKEEPER_INI=os.path.join(sb, "no.ini")), \
            patched(os, "replace", _n9_flaky(os.replace, None, [])), patched(store.time, "sleep", keep_sleep):
        ok2, why2 = store.ensure_store(os.path.join(vol2, real_login()), settings())
    t.check("label retry (control): the rename always collides -> the store is refused, the reason says PermissionError, no temporary file left",
            not ok2 and "PermissionError" in why2 and not parts(vol2), (ok2, why2, listing(vol2)))
    # --- README: the same form (os.rename)
    d4 = os.path.join(sb, "d4")
    os.makedirs(d4)
    calls = []
    with patched(os, "rename", _n9_flaky(os.rename, 3, calls)), patched(store.time, "sleep", keep_sleep):
        ok4, why4 = store._write_readme(d4, "hello\r\n")
    t.check("README retry: 3 PermissionErrors then success -> written, no temporary file", ok4 and os.path.isfile(os.path.join(d4, "README.txt"))
            and len(calls) == 4 and not parts(d4), (ok4, why4, listing(d4)))
    d5 = os.path.join(sb, "d5")
    os.makedirs(d5)
    calls = []
    with patched(os, "rename", _n9_flaky(os.rename, None, calls)), patched(store.time, "sleep", keep_sleep):
        ok5, why5 = store._write_readme(d5, "hello\r\n")
    t.check("README retry (control): always PermissionError -> (False, reason) after PLACE_TRIES tries, no temporary file",
            not ok5 and "PermissionError" in why5 and len(calls) == store.PLACE_TRIES and not parts(d5), (ok5, why5, len(calls), listing(d5)))


def n10_race(sb, pre_tool, rounds=5, procs=12):
    """12 hooks (`procs`) at once on an empty volume, `rounds` times, the hook `pre_tool`. -> (refused, leftovers):
    what was refused (first 200 characters of each answer) and what was left over (temporary labels, folders renamed away,
    the folders `.<login>.part-*` of the hooks that lost the race, a round without a personal folder)."""
    login = real_login()
    f = write(os.path.join(sb, "p", "a.txt"), "x\n")
    ev = json.dumps(edit_event(f)).encode("utf-8")
    refused, leftovers = [], []
    for rnd in range(rounds):
        vol = os.path.join(sb, "vol%d" % rnd)
        e = clean_env()
        e.update({"SHEDKEEPER_STORE_ROOT": vol, "SHEDKEEPER_INI": os.path.join(sb, "no.ini")})
        ps = [subprocess.Popen([PY, pre_tool], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e)
              for _ in range(procs)]
        for p in ps:
            p.stdin.write(ev)
            p.stdin.close()
        outs = [(p.stdout.read().decode("utf-8", "replace"), p.wait()) for p in ps]
        refused += [o[:200] for o, rc in outs if rc != 0 or verdict(o) == "deny"]
        leftovers += [n for n in listing(vol) if ".part-" in n or ".rejected-" in n]
        if not os.path.isdir(os.path.join(vol, login)):
            leftovers.append("no personal folder in round %d" % rnd)
        time.sleep(0.3)
    return refused, leftovers


def t_n9_race_twelve_hooks(t):
    """K2, a PROBABILISTIC check (not deterministic: it can be green by luck): 12 hooks at once on an empty volume, 5 rounds.
    Measured by the authors: 7 refusals in 300 starts before the retry, 12 in 300 with the retry alone; 0 in 300 with the creation lock
    (the creation lock). The deterministic proofs are `t_n9_label_retry` and `t_n10_creation_lock`."""
    refused, leftovers = n10_race(sandbox("n9r"), PRE_TOOL)
    t.check("race (probabilistic): 12 hooks at once x 5 rounds on an empty volume -- none refused", not refused, refused[:3])
    t.check("race (probabilistic): ... one personal folder per round, no temporary label left", not leftovers, leftovers)


def t_n10_creation_lock(t):
    """the creation lock: the first creation of a store goes one process at a time (a named mutex per volume folder); the wait is bounded;
    a mutex that cannot be had is not a refusal, and the log says so. Deterministic: threads and a held creation step."""
    sb = sandbox("n10l")
    ini = os.path.join(sb, "no.ini")
    # --- turns: A is in the middle of creating the personal folder, B starts then
    vol = os.path.join(sb, "vol")
    pers = os.path.join(vol, real_login())
    inside, go = threading.Event(), threading.Event()
    entered, results = [], {}
    real_create = store._create_personal

    def slow_create(root, st):
        entered.append(threading.current_thread().name)
        inside.set()
        go.wait(15)
        return real_create(root, st)

    def run(name):
        results[name] = store.ensure_store_report(pers, settings())

    with env(SHEDKEEPER_STORE_ROOT=vol, SHEDKEEPER_INI=ini), patched(store, "_create_personal", slow_create):
        a = threading.Thread(target=run, args=("a",), name="a")
        a.start()
        got_inside = inside.wait(15)
        b = threading.Thread(target=run, args=("b",), name="b")
        b.start()
        b.join(0.8)
        b_waits, entered_while = b.is_alive(), list(entered)
        go.set()
        a.join(20)
        b.join(20)
    t.check("creation lock: A is in the middle of creating the store (the step is held)", got_inside, entered)
    t.check("creation lock: B, started meanwhile, WAITS -- it is not finished and has not entered the creation step",
            b_waits and entered_while == ["a"], (b_waits, entered_while))
    ra, rb = results.get("a"), results.get("b")
    t.check("creation lock: after A is done both are fine, nobody is refused", bool(ra and rb and ra[0] and rb[0]), (ra, rb))
    t.check("creation lock: the store was created ONCE (by A); B found it ready and made nothing; no leftover folders",
            entered == ["a"] and ra and ra[2] is not None and rb and rb[2] is None
            and not [n for n in listing(vol) if ".part-" in n or ".rejected-" in n], (entered, listing(vol)))
    # --- a mutex that cannot be had: not a refusal, the log says why
    real_open, real_wait = store._open_mutex, store._wait_mutex

    def no_mutex(name):
        raise OSError(errno.EACCES, "denied")

    cases = [("cannot be made", {"_open_mutex": no_mutex}, "could not be made"),
             ("not got in time", {"_wait_mutex": lambda h, ms: 0x102}, "was not got"),
             ("left by a dead owner", {"_wait_mutex": lambda h, ms: (real_wait(h, ms), 0x80)[1]}, "died in the middle")]
    for i, (what, patch, word) in enumerate(cases):
        v = os.path.join(sb, "vm%d" % i)
        pe = os.path.join(v, real_login())
        with env(SHEDKEEPER_STORE_ROOT=v, SHEDKEEPER_INI=ini):
            for name, fn in patch.items():
                setattr(store, name, fn)
            try:
                ok, why, rep = store.ensure_store_report(pe, settings())
            finally:
                store._open_mutex, store._wait_mutex = real_open, real_wait
        logtext = open(os.path.join(pe, "log.jsonl"), encoding="utf-8").read() if os.path.isfile(os.path.join(pe, "log.jsonl")) else ""
        t.check("creation lock (%s): the store is made all the same, and the log names it" % what,
                ok and os.path.isdir(pe) and "creation_lock" in logtext and word in logtext, (ok, why, logtext[:300]))
    # --- the lock is released, and a ready store does not take it
    v = os.path.join(sb, "vr")
    pe = os.path.join(v, real_login())
    with env(SHEDKEEPER_STORE_ROOT=v, SHEDKEEPER_INI=ini):
        ok0 = store.ensure_store_report(pe, settings())[0]
        box = {}

        def grab():
            with store._creation_lock(v, 0.5) as lk:
                box["held"] = lk.held
        th = threading.Thread(target=grab)
        th.start()
        th.join(10)
        t.check("creation lock: released after the creation (another thread gets it at once)", ok0 and box.get("held") is True, (ok0, box))
        hold, free = threading.Event(), threading.Event()

        def holder():
            with store._creation_lock(v, 5.0):
                hold.set()
                free.wait(15)
        ht = threading.Thread(target=holder)
        ht.start()
        hold.wait(10)
        t0 = time.time()
        with patched(store, "CREATE_LOCK_WAIT_S", 1.0):
            ok1, why1, rep1 = store.ensure_store_report(pe, settings())
        took = time.time() - t0
        free.set()
        ht.join(10)
    t.check("creation lock: a store that is READY is passed without the lock (the lock is held elsewhere, no wait, no log line)",
            ok1 and rep1 is None and took < 0.5, (ok1, why1, took))


def t_n11_refusal_note(t):
    """the refusal note: when the creation lock was not got AND the creation refuses, the reason of the lock is in the refusal text
    (at the first creation there is no journal yet to carry it); the wait is short (3 s) -- it is outside the time budgets."""
    sb = sandbox("n11n")
    ini = os.path.join(sb, "no.ini")
    v = os.path.join(sb, "vn")
    pe = os.path.join(v, real_login())
    with env(SHEDKEEPER_STORE_ROOT=v, SHEDKEEPER_INI=ini), \
            patched(store, "_wait_mutex", lambda h, ms: 0x102), \
            patched(store, "_create_personal", lambda root, st: (False, "creation refused: test", None)):
        ok, why, rep = store.ensure_store_report(pe, settings())
    t.check("refusal note: the creation refused and the lock was not got -> the refusal text carries the lock note",
            (not ok) and "creation refused: test" in why and "was not got" in why and rep is None, (ok, why))
    with env(SHEDKEEPER_STORE_ROOT=v, SHEDKEEPER_INI=ini), \
            patched(store, "_create_personal", lambda root, st: (False, "creation refused: test", None)):
        ok2, why2, _r = store.ensure_store_report(os.path.join(sb, "vn2", real_login()), settings())
    t.check("refusal note: the lock WAS got -> the refusal text is unchanged (no lock note added)",
            (not ok2) and why2 == "creation refused: test", (ok2, why2))
    t.check("creation lock: the wait is at most 3 s (outside cmd_deadline_s and the git budget)",
            store.CREATE_LOCK_WAIT_S <= 3.0, store.CREATE_LOCK_WAIT_S)


def t_mutations_n11(t):
    """the refusal note: the lock note is not added to the refusal -> the refusal-note test must turn red."""
    import inspect
    src = inspect.getsource(store.ensure_store_report)
    old = '            result = (False, "%s (%s)" % (result[1], lk.note), result[2])'
    if old not in src:
        t._add("NOT MEASURED", "mutation n11", "the place to break was not found in ensure_store_report")
        return
    mutated = src.replace(old, "            pass")

    @contextlib.contextmanager
    def m_no_note():
        real = store.ensure_store_report
        exec(compile(mutated, "<mutated ensure_store_report>", "exec"), store.__dict__)
        try:
            yield
        finally:
            store.ensure_store_report = real

    run_mutation(t, "the lock note is not added to the refusal", m_no_note, [t_n11_refusal_note])


def n10_mutated_kit(sb):
    """A copy of the kit in which the creation lock is never taken -> the path of its pre_tool.py."""
    dst = os.path.join(sb, "kit_mut")
    copy_kit(dst)
    sp = os.path.join(dst, "shedkeeper", "store.py")
    with io.open(sp, encoding="utf-8", newline="") as fh:
        src = fh.read()
    old = "    return _CreationLock(vol, CREATE_LOCK_WAIT_S if wait_s is None else wait_s)"
    if old not in src:
        raise AssertionError("the place to break was not found in store.py")
    new = ("    class _NoLock(object):\n        held, note = False, ''\n        def __enter__(self): return self\n"
           "        def __exit__(self, *e): return False\n    return _NoLock()")
    with io.open(sp, "w", encoding="utf-8", newline="") as fh:
        fh.write(src.replace(old, new))
    return os.path.join(dst, "hooks", "pre_tool.py")


def t_mutations_n10(t):
    """the creation lock: a guarded place is broken in memory and the matching test must turn red."""
    class NoLock(object):
        held, note = False, ""

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    def m_no_lock():                     # the turns are never taken
        return patched(store, "_creation_lock", lambda vol, wait_s=None: NoLock())

    def m_never_released():              # the mutex is not released (and not closed)
        return patched(store, "_release_mutex", lambda h, owned: None)

    def m_no_fast_path():                # a ready store goes through the lock too
        return patched(store, "_store_ready", lambda root, settings: False)

    run_mutation(t, "the creation lock is never taken", m_no_lock, [t_n10_creation_lock])
    run_mutation(t, "the creation lock is never released", m_never_released, [t_n10_creation_lock])
    run_mutation(t, "a ready store takes the lock too (no fast path)", m_no_fast_path, [t_n10_creation_lock])
    # the probabilistic check on a copy of the kit without the lock: it is NOT a proof (the race can be missed by luck): the verdict
    # is what the run shows, either way
    sb = sandbox("n10m")
    refused, leftovers = n10_race(sb, n10_mutated_kit(sb), rounds=8)
    t.check("mutation: 12 hooks x 8 rounds (probabilistic) on a kit copy WITHOUT the lock -- the race check turns red: CAUGHT",
            bool(refused or leftovers), "the run saw no refusal and no leftover: the race was missed by luck, not proved absent")


def t_mutations_n9(t):
    """the staging findings: a guarded place is broken in memory and the matching test must turn red."""
    def m_no_retry():                    # one try, as before
        return patched(store, "PLACE_TRIES", 1)

    def m_temp_stays():                  # our temporary file is not removed after the last failed try
        return patched(store, "_drop_own_temp", lambda tmp: None)

    def m_retry_any_error():             # every OSError is taken for a collision
        def place(tmp, dst, op="replace"):
            for _ in range(store.PLACE_TRIES):
                try:
                    getattr(os, op)(tmp, dst)
                    return
                except OSError:
                    pass
            store._drop_own_temp(tmp)
            raise OSError(errno.EIO, "disk error")
        return patched(store, "_place", place)

    run_mutation(t, "the label rename is tried once (no retry on PermissionError)", m_no_retry, [t_n9_label_retry])
    run_mutation(t, "our temporary label file stays after the refusal", m_temp_stays, [t_n9_label_retry])
    run_mutation(t, "any OSError is retried like a collision", m_retry_any_error, [t_n9_label_retry])


def t_n6_disable_all_hooks(t):
    """Item 5. disableAllHooks is read over ALL the settings that apply, by precedence -- not from the one file `check` was pointed at."""
    m = install_mod()
    sb = sandbox("n6h")
    home = os.path.join(sb, "home")
    proj = os.path.join(sb, "proj")
    cl = os.path.join(proj, ".claude")
    os.makedirs(os.path.join(home, ".claude"))
    os.makedirs(cl)
    loc, shared, user = os.path.join(cl, "settings.local.json"), os.path.join(cl, "settings.json"), os.path.join(home, ".claude", "settings.json")

    def run(*argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = m.main(list(argv))
        return rc, buf.getvalue()

    with n6_store_env(sb), env(SHEDKEEPER_USER_HOME=home, CLAUDE_PROJECT_DIR=None):
        m.install(shared)
        rc, out = run("check", "--project", proj)
        t.check("disableAllHooks: nowhere set -> check is green (the control)", rc == 0 and "disableAllHooks" not in out, (rc, out[-400:]))
        write(loc, json.dumps({"disableAllHooks": True}))
        rc, out = run("check", "--project", proj)
        t.check("disableAllHooks: true in settings.local.json while `check` reads settings.json -> exit 1, the file is named",
                rc == 1 and "disableAllHooks is TRUE in %s" % loc in out, (rc, out[-500:]))
        write(loc, json.dumps({"disableAllHooks": False}))
        write(user, json.dumps({"disableAllHooks": True}))
        rc, out = run("check", "--project", proj)
        t.check("disableAllHooks: true in the user file but false in settings.local.json (higher) -> green, and it is said that it has no effect",
                rc == 0 and "has no effect" in out and "disableAllHooks is TRUE" not in out, (rc, out[-500:]))
        write(loc, "{}")
        rc, out = run("check", "--project", proj)
        t.check("disableAllHooks: true in the user file only -> exit 1, the user file is named", rc == 1 and "disableAllHooks is TRUE in %s" % user in out, (rc, out[-500:]))
        rc, out = run("check", "--user")
        t.check("disableAllHooks: `check --user` reads the user file", rc == 1 and "disableAllHooks is TRUE in %s" % user in out, (rc, out[-300:]))


def t_n6_git_bash(t):
    """Item 6. `install` finds Git Bash the way Claude Code does, and refuses -- with the reason and the link to the documentation --
    when it is not there: a hook command in a shell that is not Git Bash does nothing, silently."""
    m = install_mod()
    sb = sandbox("n6g")
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    sp = os.path.join(proj, ".claude", "settings.json")
    home = os.path.join(sb, "home")
    os.makedirs(os.path.join(home, ".claude"))
    with n6_store_env(sb), env(SHEDKEEPER_USER_HOME=home, CLAUDE_PROJECT_DIR=None):
        with patched(m, "find_bash", lambda: None):
            rc, lines = m.install(sp)
            out = "\n".join(lines)
            t.check("no Git Bash: install() is REFUSED (exit 1) with the reason, the download link and the documentation page; nothing is written",
                    rc == 1 and out.startswith("REFUSED: Git Bash was not found") and "https://git-scm.com/downloads/win" in out
                    and "https://code.claude.com/docs/en/troubleshooting" in out and "CLAUDE_CODE_GIT_BASH_PATH" in out and not os.path.exists(sp), (rc, out[:300]))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = m.main(["install", "--project", proj])
            t.check("no Git Bash: `install --project` (the command line) says it and exits 1", rc == 1 and "Git Bash was not found" in buf.getvalue() and not os.path.exists(sp), (rc, buf.getvalue()[:200]))
        rc, lines = m.install(sp)
        t.check("with Git Bash (the control): the same install goes", rc == 0 and os.path.isfile(sp), (rc, lines))
        with patched(m, "find_bash", lambda: None):
            rc, lines = m.check(sp, proj)
            t.check("no Git Bash: check fails and says Git Bash was not found (the probe cannot run)", rc == 1 and any("Git Bash was not found" in x for x in lines), lines[-4:])
            rc, lines = m.uninstall(sp)
            t.check("no Git Bash: uninstall is not refused (taking the lines out needs no shell)", rc == 0 and not any(m.is_ours(c) for c in all_commands(jload(sp))), (rc, lines))


def t_n6_shared_file(t):
    """Item 1 (the end). `install --project` on a settings.json that is shared through git warns, and goes on."""
    m = install_mod()
    git = shutil.which("git")
    if not git:
        raise NotMeasured("git was not found: the warning about a shared settings file was NOT measured")
    sb = sandbox("n6s")
    home = os.path.join(sb, "home")
    os.makedirs(os.path.join(home, ".claude"))

    def repo(name, tracked=False, ignored=False, init=True):
        proj = os.path.join(sb, name)
        os.makedirs(os.path.join(proj, ".claude"))
        sp = os.path.join(proj, ".claude", "settings.json")
        write(sp, "{}\n")
        if init:
            subprocess.run([git, "init", "-q"], cwd=proj, check=True, capture_output=True)
            if ignored:
                write(os.path.join(proj, ".gitignore"), ".claude/settings.json\n")
            if tracked:
                subprocess.run([git, "add", "-f", ".claude/settings.json"], cwd=proj, check=True, capture_output=True)
        return proj, sp

    with n6_store_env(sb), env(SHEDKEEPER_USER_HOME=home, CLAUDE_PROJECT_DIR=None):
        proj, sp = repo("tracked", tracked=True)
        w = m.shared_warning(sp)
        t.check("shared: a settings.json TRACKED by git -> a WARNING that it is committed, why that matters, and what to do instead (--user / settings.local.json)",
                bool(w) and w.startswith("WARNING:") and "committed to git" in w and "--user" in w and "settings.local.json" in w and KIT in w, w)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = m.main(["install", "--project", proj])
        out = buf.getvalue()
        t.check("shared: `install --project` prints the warning BEFORE it writes, and still installs (exit 0, entries added)",
                rc == 0 and out.index("WARNING:") < out.index("PreToolUse/pre_tool.py: added") and any(m.is_ours(c) for c in all_commands(jload(sp))), (rc, out[:400]))
        proj, sp = repo("untracked")
        w = m.shared_warning(sp)
        t.check("shared: inside a working tree and NOT ignored -> warned (it is shared as soon as it is committed)", bool(w) and "not ignored by git" in w, w)
        proj, sp = repo("ignored", ignored=True)
        t.check("shared: ignored by git -> no warning (it is not shared)", m.shared_warning(sp) is None, m.shared_warning(sp))
        proj, sp = repo("norepo", init=False)
        probe = subprocess.run([git, "rev-parse", "--is-inside-work-tree"], cwd=proj, capture_output=True)
        if probe.returncode == 0:
            raise NotMeasured("the temporary folder is inside a git working tree: the 'no repository' case was NOT measured")
        t.check("shared: not in a git repository -> no warning", m.shared_warning(sp) is None, m.shared_warning(sp))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            m.main(["install", "--user"])
        t.check("shared: `install --user` never prints the warning", "WARNING:" not in buf.getvalue(), buf.getvalue()[:200])


def t_n6_not_measured(t):
    """Item 7. A test that cannot be measured because Git Bash is not there is NOT a green SKIP: it is 'NOT MEASURED', the total says so,
    and the run ends with exit code 2."""
    inner = T(quiet=True)

    def needs_a_tool(tt):
        raise NotMeasured("the tool is not there")

    inner.run(needs_a_tool)
    t.check("NOT MEASURED: the row has its own status (not PASS, not SKIP) and is neither a failure nor a crash",
            inner.rows and inner.rows[0][0] == "NOT MEASURED" and not inner.fails() and not inner.crashes(), inner.rows)
    t.check("exit code: all green -> 0", exit_code({"PASS": 5}) == 0)
    t.check("exit code: all green and a SKIP -> 0 (SKIP stays what it was: a legitimate 'does not apply here')", exit_code({"PASS": 5, "SKIP": 2}) == 0)
    t.check("exit code: NOT MEASURED and nothing red -> 2", exit_code({"PASS": 5, "NOT MEASURED": 1}) == 2)
    t.check("exit code: a failure wins over NOT MEASURED -> 1", exit_code({"FAIL": 1, "NOT MEASURED": 1}) == 1 and exit_code({"CRASH": 1}) == 1)
    outer = T(quiet=True)
    run_mutation(outer, "x", lambda: contextlib.nullcontext(), [needs_a_tool])
    t.check("a mutation whose guarding test could not be measured is NOT MEASURED, not 'SLIPPED' and not 'CAUGHT'",
            outer.rows and outer.rows[0][0] == "NOT MEASURED", outer.rows)
    # the two key tests of the commands in Git Bash, with Git Bash taken away
    m = install_mod()
    with patched(m, "find_bash", lambda: None):
        probe = T(quiet=True)
        probe.run(t_n4_command_runs_in_git_bash)
        probe.run(t_n5_command_in_git_bash)
    t.check("without Git Bash the two tests that run the command in it are NOT MEASURED (they were SKIP, and the run ended green)",
            [r[0] for r in probe.rows] == ["NOT MEASURED", "NOT MEASURED"], probe.rows)


def t_n6_docs(t):
    """Item 8. The README says what the code does -- with the conditions."""
    readme = " ".join(read(README).split())
    t.check("README: 'nothing runs twice' carries its conditions (the normal case; CLAUDE_PROJECT_DIR; a live entry; disableAllHooks; subfolder, --plugin-dir, managed settings)",
            "in the normal case nothing runs twice" in readme and "only when all of these hold" in readme and "CLAUDE_PROJECT_DIR" in readme
            and "disableAllHooks" in readme and "--plugin-dir" in readme and "managed (enterprise) settings" in readme and "both run" in readme, "missing")
    t.check("README: a shared project file is not for a personal install (what install says, what to do)",
            "A shared project file is not for a personal install" in readme and "settings.local.json" in readme and "--user" in readme, "missing")
    t.check("README: the record -- what is written, where, why, and what happens when it cannot be written",
            "installs.jsonl" in readme and "the proof that a line is the installer's own" in readme and "will have to be taken out by hand" in readme, "missing")
    t.check("README: the plugin door is a PROOF (order of the files, registry, hook files) and `plugin door NOT PROVEN` is named",
            "enabled **and installed**" in readme and "project-local, then project, then user" in readme and "plugin door NOT PROVEN" in readme, "missing")
    t.check("README: CLAUDE_CONFIG_DIR, Git Bash refusal, what the probe proves and does not",
            "CLAUDE_CONFIG_DIR" in readme and "`install` refuses" in readme and "the hook starts and answers" in readme, "missing")
    t.check("README: the limits are named (a plugin folder with $ or a backtick; project scope shares the setting, not the installation; what is not covered)",
            "holds a `$` or a backtick" in readme and "the setting is shared; the installation is not" in readme and "Not measured, not covered" in readme, "missing")
    t.check("README: the old unconditional claim is gone", "nothing runs twice: `python install.py install` refuses" not in readme)
    t.check("README: the dead-line rule says 'by hand' for what the installer did not write", "remove it by hand" in readme and "never taken out by the tool" in readme, "missing")


def t_mutations_n6(t):
    """proof instead of looks: a guarded place is broken in memory and the matching test must turn red."""
    m = install_mod()

    def m_no_record_needed():           # a dead line is recognised without the record of the installer
        real = m.classify_dead

        def dead(command, recorded=None):
            return real(command, {command})
        return patched(m, "classify_dead", dead)

    def m_denied_is_gone():             # a refusal of access is taken for a missing file
        return patched(m, "_absence", lambda p: "present" if os.path.exists(p) else "absent")

    def m_one_line_is_enough():         # the plugin counts as enabled by one line of the settings
        def status(project=None):
            return [(p, k, "?") for p, k in m.plugin_enabled(m.applicable_settings(project))], []
        return patched(m, "plugin_status", status)

    def m_yield_by_cwd():               # yield by the cwd of the event, as before (no CLAUDE_PROJECT_DIR needed)
        def yields(event, cwd=None):
            project = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
            return m.live_entries(project).get(event, []) if project else []
        return patched(m, "plugin_yields", yields)

    def m_yield_despite_disabled():     # disableAllHooks is not looked at when yielding
        def yields(event, cwd=None):
            project = os.environ.get("CLAUDE_PROJECT_DIR")
            return m.live_entries(project).get(event, []) if project else []
        return patched(m, "plugin_yields", yields)

    def m_yield_without_live_entry():   # the plugin yields whenever the variable is there
        return patched(m, "plugin_yields", lambda event, cwd=None: ["x"] if os.environ.get("CLAUDE_PROJECT_DIR") else [])

    def m_config_dir_ignored():         # CLAUDE_CONFIG_DIR is not read
        return patched(m, "user_claude_dir", lambda: os.path.join(os.environ.get("SHEDKEEPER_USER_HOME") or os.path.expanduser("~"), ".claude"))

    def m_hooks_disabled_one_file():    # disableAllHooks from the user file only
        def disabled(project=None):
            p = os.path.join(m.user_claude_dir(), "settings.json")
            data, err = m._load(p)
            v = data.get("disableAllHooks") if data and not err else None
            return (v if isinstance(v, bool) else None), (p if v else None), ([p] if v else [])
        return patched(m, "hooks_disabled", disabled)

    def m_local_order():                # the lowest-precedence file is read first (project settings.json over settings.local.json)
        return patched(m, "applicable_settings", lambda project=None: ([os.path.join(os.path.abspath(project), ".claude", n) for n in ("settings.json", "settings.local.json")] if project else []) + [os.path.join(m.user_claude_dir(), "settings.json")])

    def m_no_bash_check():              # install does not look for Git Bash (it goes on as if it were found)
        real = m.install

        def install_anyway(path, add_entries=True):
            with patched(m, "find_bash", lambda: "C:/never/bash.exe"):
                return real(path, add_entries)
        return patched(m, "install", install_anyway)

    run_mutation(t, "a dead line is recognised without the record of the installer", m_no_record_needed, [t_n6_record_proof])
    run_mutation(t, "a refusal of access is taken for a missing file", m_denied_is_gone, [t_n6_absence])
    run_mutation(t, "the plugin counts as enabled by one line of the settings", m_one_line_is_enough, [t_n6_plugin_proof, t_n5_installer_refuses])
    run_mutation(t, "the plugin yields by the cwd, without CLAUDE_PROJECT_DIR", m_yield_by_cwd, [t_n6_yield_proof])
    run_mutation(t, "the plugin yields although disableAllHooks is on", m_yield_despite_disabled, [t_n6_yield_proof])
    run_mutation(t, "the plugin yields without a live entry of install.py (the variable is enough)", m_yield_without_live_entry, [t_n6_yield_proof, t_n5_two_doors_runtime])
    run_mutation(t, "CLAUDE_CONFIG_DIR is not read", m_config_dir_ignored, [t_n6_config_dir])
    run_mutation(t, "disableAllHooks is read from one file only", m_hooks_disabled_one_file, [t_n6_disable_all_hooks])
    run_mutation(t, "the settings files are read in the wrong order of precedence", m_local_order, [t_n6_plugin_proof])
    run_mutation(t, "install does not look for Git Bash", m_no_bash_check, [t_n6_git_bash])


# =============================================================================================
# the independent-review findings: the five findings of an independent review
# =============================================================================================

@contextlib.contextmanager
def n12_racing(target, content):
    """Every rename / replace whose destination is `target` first creates `target` with `content` when it is not there:
    a file that appears between the check of the place and the last step of a restore. Both calls are wrapped, so the test
    does not depend on which of them the code uses."""
    real = (os.replace, os.rename)

    def wrap(fn):
        def racing(src, dst, *a, **k):
            if os.path.normcase(os.path.abspath(str(dst))) == os.path.normcase(os.path.abspath(target)) \
                    and not os.path.lexists(target):
                with open(target, "wb") as fh:
                    fh.write(content)
            return fn(src, dst, *a, **k)
        return racing
    os.replace, os.rename = wrap(real[0]), wrap(real[1])
    try:
        yield
    finally:
        os.replace, os.rename = real


def t_n12_c1_restore_race(t):
    """C1: a file that appears at the target between the check and the last step of a restore is not overwritten."""
    sb = sandbox("n12c1")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "p", "a.txt"), "old")
        store.trash(f, "", st, "", NOW)
        with n12_racing(f, b"RACER"):
            status, msg = store.restore("t:2026-09-30:1", st, now=NOW)
        t.check("C1 trash: a file that appeared at the target during the restore is refused, not overwritten",
                status == "refused" and "taken" in msg and read(f) == "RACER", (status, msg))
        hits, _p = store.find("a.txt", st)
        t.check("C1 trash: the item is still in the trash (nothing was lost)",
                [h for h in hits if h["kind"] == "trash" and h["present"]], hits)
        g = write(os.path.join(sb, "p", "doc.md"), "v1\n")
        r = store.copy_version(g, root, st, "", "Edit", NOW)
        os.remove(g)
        with n12_racing(g, b"RACER"):
            status, msg = store.restore(r["id"], st, now=NOW)
        parts = [n for n in os.listdir(os.path.dirname(g)) if n.endswith(".part")]
        t.check("C1 version, place free: the racer stays, the restore refuses, the verified copy stays as a .part file",
                status == "refused" and "taken" in msg and read(g) == "RACER" and len(parts) == 1
                and read(os.path.join(os.path.dirname(g), parts[0])) == "v1\n", (status, msg, parts))
        h = write(os.path.join(sb, "p", "doc3.md"), "v1\n")
        r3 = store.copy_version(h, root, st, "", "Edit", NOW)
        write(h, "occupied now\n")
        # the place is occupied at the check (that file goes to the trash), then ANOTHER file appears before the last step
        real_trash = store.trash

        def trash_then_race(*a, **k):
            res = real_trash(*a, **k)
            with open(h, "wb") as fh:
                fh.write(b"RACER2")
            return res
        with patched(store, "trash", trash_then_race):
            status, msg = store.restore(r3["id"], st, now=NOW)
        t.check("C1 version, place occupied: the second racer stays, the restore refuses and says the displaced file could not be put back",
                status == "refused" and read(h) == "RACER2" and "NOT be put back" in msg, (status, msg))
        hits, _p = store.find("doc3.md", st)
        t.check("C1 version, place occupied: the displaced file is in the trash with its content",
                [x for x in hits if x["kind"] == "trash" and read(x["where"]) == "occupied now\n"], hits)


def t_n12_c2_session_id(t):
    """C2: a session id that is not a plain name never leaves the transcripts folder; the hook does not fail and says so in the journal."""
    sb = sandbox("n12c2")
    with store_env(sb) as (root, st):
        src = write(os.path.join(sb, "cc", "proj-x", "s.jsonl"), "1\n")
        os.makedirs(root, exist_ok=True)
        for bad in ("../../../escaped", "..\\..\\escaped", "a/b", "C:evil", "", "x" * 129, "has space", "a.b", None, 5):
            r = store.save_transcript(src, root, "proj-x", bad)
            t.check("C2: session id %r -> failed, nothing copied" % (bad,), isinstance(r, str) and r.startswith("failed"), r)
        outside = [os.path.join(dp, f) for dp, _d, fs in os.walk(sb) for f in fs if f.startswith("escaped") or f.startswith("evil")]
        t.check("C2: no file named by a bad id exists anywhere in the sandbox", not outside, outside)
        uuid = "3a2b8f9a-1c4d-4e5f-8a6b-7c8d9e0f1a2b"
        t.check("C2: a uuid-shaped id (and 128 characters of the allowed set) is taken",
                store.save_transcript(src, root, "proj-x", uuid) == "copied"
                and store.save_transcript(src, root, "proj-x", "A_" + "z" * 126) == "copied")
    home = os.path.join(sb, "home")
    os.makedirs(home)
    proj = os.path.join(sb, "proj")
    os.makedirs(proj)
    tp = write(os.path.join(sb, "cc2", "projects", "pf", "abc.jsonl"), "1\n")
    with store_env(sb) as (root, st), env(SHEDKEEPER_USER_HOME=home):
        ev = {"hook_event_name": "SessionEnd", "transcript_path": tp, "session_id": "../../../escaped2", "cwd": proj}
        rc, out, err = hook_sub(SESSION, ev)
        logf = os.path.join(root, "log.jsonl")
        logged = read(logf) if os.path.isfile(logf) else ""
        t.check("C2 hook: a bad session_id in the hook input -> exit 0, no traceback, nothing outside, a journal line",
                rc == 0 and "Traceback" not in err and not [1 for dp, _d, fs in os.walk(sb) for f in fs if f.startswith("escaped2")]
                and "transcript_error" in logged and "not a plain name" in logged, (rc, err[:200], logged[-300:]))


def t_n12_c3_corrupt_last_copy(t):
    """C3: `same` only when the previous copy, read now, equals the source; a damaged one -> a new copy and a journal line."""
    sb = sandbox("n12c3")
    with store_env(sb) as (root, st):
        f = write(os.path.join(sb, "p", "doc.txt"), "original\n")
        r1 = store.copy_version(f, root, st, "", "Edit", NOW)
        r1b = store.copy_version(f, root, st, "", "Edit", NOW)
        t.check("C3 control: an intact last copy -> same", r1["status"] == "ok" and r1b["status"] == "same", (r1, r1b))
        write(r1["copy"], "CORRUPT!\n")
        r2 = store.copy_version(f, root, st, "", "Edit", NOW)
        t.check("C3: the last copy damaged (same size) -> a NEW copy with the source content, not `same`",
                r2["status"] == "ok" and r2["copy"] != r1["copy"] and read(r2["copy"]) == "original\n", r2)
        t.check("C3: the damaged copy is left as it is (never overwritten), the journal says it did not match",
                read(r1["copy"]) == "CORRUPT!\n" and "last_copy_mismatch" in read(os.path.join(root, "log.jsonl")), r1)
        write(r2["copy"], "short")
        r3 = store.copy_version(f, root, st, "", "Edit", NOW)
        t.check("C3: the last copy truncated -> a new copy", r3["status"] == "ok" and read(r3["copy"]) == "original\n", r3)
        r4 = store.copy_version(f, root, st, "", "Edit", NOW)
        t.check("C3: after that the new copy is `same` again", r4["status"] == "same" and r4["copy"] == r3["copy"], r4)


def t_n12_c4_door_when_hooks_disabled(t):
    """C4: disableAllHooks on -> `door: none` naming the file, `check` exits 1; off -> the door is named as before."""
    m = install_mod()
    sb = sandbox("n12c4")
    home = os.path.join(sb, "home")
    proj = os.path.join(sb, "proj")
    os.makedirs(os.path.join(home, ".claude"))
    os.makedirs(os.path.join(proj, ".claude"))
    shared = os.path.join(proj, ".claude", "settings.json")

    def run(*argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = m.main(list(argv))
        return rc, buf.getvalue()

    with n6_store_env(sb), env(SHEDKEEPER_USER_HOME=home, CLAUDE_PROJECT_DIR=None):
        m.install(shared)
        lines = m.door_lines(m.door_state(proj))
        t.check("C4 control: hooks not disabled -> `door: install.py`", lines and lines[0].startswith("door: install.py"), lines)
        d = jload(shared)
        d["disableAllHooks"] = True
        write(shared, json.dumps(d))
        lines = m.door_lines(m.door_state(proj))
        t.check("C4: disableAllHooks true -> the door line says `door: none`, names disableAllHooks and the file, and no `door: install.py`",
                lines and lines[0].startswith("door: none -- disableAllHooks is on in %s" % shared)
                and not any(x.startswith("door: install.py") for x in lines), lines)
        rc, out = run("check", "--project", proj)
        t.check("C4: `check` exits 1 and its door line is the disabled one", rc == 1 and "door: none -- disableAllHooks" in out
                and "door: install.py" not in out, (rc, out[-500:]))
        write(os.path.join(proj, ".claude", "settings.local.json"), json.dumps({"disableAllHooks": False}))
        lines = m.door_lines(m.door_state(proj))
        t.check("C4: false in settings.local.json (higher precedence) beats true in settings.json -> the door is named as before",
                lines and lines[0].startswith("door: install.py"), lines)


def t_n12_c5_uninstall_exact(t):
    """C5: uninstall takes out only the exact commands this installer writes for this kit (or recorded ones); a line that merely
    ends with a hook path stays and is named."""
    m = install_mod()
    sb = sandbox("n12c5")
    sp = os.path.join(sb, "proj", ".claude", "settings.json")
    hook = os.path.join(KIT, "hooks", "pre_tool.py").replace("\\", "/")
    foreign = "echo colleague-note " + hook
    wrapper = "bash -c \"log; python '%s'\"" % hook
    with n6_store_env(sb):
        m.install(sp)
        d = jload(sp)
        d["hooks"]["PreToolUse"].append({"matcher": m.PRE_MATCHER, "hooks": [{"type": "command", "command": foreign, "timeout": 60}]})
        d["hooks"]["PreToolUse"].append({"hooks": [{"type": "command", "command": wrapper}]})
        write(sp, json.dumps(d, indent=2))
        rc, lines = m.uninstall(sp)
        after = set(all_commands(jload(sp)))
        out = "\n".join(lines)
        t.check("C5: the colleague's line that ends with the hook path stays; the kit's own lines are gone",
                rc == 0 and after == {foreign, wrapper}, (rc, after))
        t.check("C5: the output names the line it left (the person decides)",
                "left a line that ends with the path of a hook of this kit" in out and foreign in out, out)
        # an own exact command is taken out also when the record is gone (exact match of the written command)
        m.install(sp)
        rec = os.path.join(m._personal_root(sp)[2], "installs.jsonl")
        write(rec, "")
        rc, lines = m.uninstall(sp)
        t.check("C5: the exact command of this installer is taken out even without a record line; the look-alikes stay",
                rc == 0 and set(all_commands(jload(sp))) == {foreign, wrapper}, (rc, all_commands(jload(sp))))
        # the old plain form of 0.1.0 / 0.2.0 for this kit is an exact form too
        d = jload(sp)
        d["hooks"]["PreToolUse"].append({"hooks": [{"type": "command", "command": 'python "%s"' % hook}]})
        write(sp, json.dumps(d))
        m.uninstall(sp)
        t.check("C5: the old plain form of this kit is still taken out", 'python "%s"' % hook not in all_commands(jload(sp)),
                all_commands(jload(sp)))


def t_n13_install_exact(t):
    """N13: `install` decides "this entry is ours" by the same exact test as `uninstall` (is_exactly_ours): a line that merely
    ends with a hook path is no installation -- not counted, not rewritten, our own entry is added beside it, the line is named."""
    m = install_mod()
    hook = os.path.join(KIT, "hooks", "pre_tool.py").replace("\\", "/")
    foreign = "echo colleague-note " + hook
    sb = sandbox("n13a")
    sp = os.path.join(sb, "proj", ".claude", "settings.json")
    with n6_store_env(sb):
        write(sp, json.dumps({"hooks": {"PreToolUse": [{"matcher": "Read", "hooks": [{"type": "command", "command": foreign, "timeout": 5}]}]}}))
        rc, lines = m.install(sp)
        out = "\n".join(lines)
        cmds = all_commands(jload(sp))
        t.check("N13: a colleague's line ending with the hook path is not 'already installed': our entry is added",
                rc == 0 and "PreToolUse/pre_tool.py: added" in out and "PreToolUse/pre_tool.py: already installed" not in out, out)
        t.check("N13: the colleague's line stays byte for byte (command, timeout, matcher); ours stands beside it",
                foreign in cmds and sum(1 for c in cmds if m.parse_command(c)) == 3
                and jload(sp)["hooks"]["PreToolUse"][0] == {"matcher": "Read", "hooks": [{"type": "command", "command": foreign, "timeout": 5}]},
                jload(sp)["hooks"]["PreToolUse"])
        t.check("N13: the output names the look-alike line", "left a line that ends with the path of a hook of this kit" in out and foreign in out, out)
        rc, lines = m.install(sp)
        t.check("N13: a second install: ours is 'already installed', the colleague's line still untouched",
                rc == 0 and "PreToolUse/pre_tool.py: already installed" in "\n".join(lines) and jload(sp)["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == foreign,
                lines)
    # a group shared with a look-alike: the matcher of the group is not rewritten
    sb = sandbox("n13b")
    sp = os.path.join(sb, "proj", ".claude", "settings.json")
    with n6_store_env(sb):
        m.install(sp)
        d = jload(sp)
        g = d["hooks"]["PreToolUse"][0]
        g["matcher"] = "Read"
        g["hooks"].append({"type": "command", "command": foreign})
        write(sp, json.dumps(d))
        rc, lines = m.install(sp)
        t.check("N13: the matcher of a group that holds a colleague's look-alike is NOT changed (said so)",
                rc == 0 and jload(sp)["hooks"]["PreToolUse"][0]["matcher"] == "Read" and "was NOT changed" in "\n".join(lines), lines)
    # an exact form naming ANOTHER copy of the kit is not ours either: untouched, ours added
    sb = sandbox("n13c")
    sp = os.path.join(sb, "proj", ".claude", "settings.json")
    other = 'python "%s"' % os.path.join(sb, "other_kit", "hooks", "pre_tool.py").replace("\\", "/")
    with n6_store_env(sb):
        write(sp, json.dumps({"hooks": {"PreToolUse": [{"matcher": "Read", "hooks": [{"type": "command", "command": other, "timeout": 7}]}]}}))
        rc, lines = m.install(sp)
        first = jload(sp)["hooks"]["PreToolUse"][0]
        t.check("N13: a line of the same form for another kit folder is left as it is; our entry is added",
                rc == 0 and first["hooks"][0] == {"type": "command", "command": other, "timeout": 7} and first["matcher"] == "Read"
                and "PreToolUse/pre_tool.py: added" in "\n".join(lines), (lines, first))
    # the readers of the same decision: check and installed_hooks do not report a look-alike as an installation
    sb = sandbox("n13d")
    sp = os.path.join(sb, "proj", ".claude", "settings.json")
    with n6_store_env(sb):
        write(sp, json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": foreign}]}]}}))
        rc, lines = m.check(sp)
        t.check("N13: `check` says NOT installed for a look-alike", any("PreToolUse/pre_tool.py: NOT installed" in x for x in lines), lines)
        t.check("N13: installed_hooks does not count a look-alike", not m.installed_hooks(extra_paths=[sp])["pre_tool"],
                m.installed_hooks(extra_paths=[sp]))


def t_mutations_n12(t):
    """the independent-review findings: one mutation per finding; each test must turn red."""
    import inspect
    m = install_mod()

    @contextlib.contextmanager
    def mutate_source(func, pairs):
        src = inspect.getsource(func)
        for old, _new in pairs:
            if old not in src:
                raise NotMeasured("the place to break was not found in %s: %s" % (func.__name__, old))
        for old, new in pairs:
            src = src.replace(old, new)
        real = getattr(store, func.__name__)
        exec(compile(src, "<mutated %s>" % func.__name__, "exec"), store.__dict__)
        try:
            yield
        finally:
            setattr(store, func.__name__, real)

    def m_c1():                 # the last step of a restore overwrites again
        return mutate_source(store._restore, [("os.rename(_lp(part), _lp(target))", "os.replace(_lp(part), _lp(target))"),
                                              ("os.rename(_lp(dst), _lp(target))", "os.replace(_lp(dst), _lp(target))")])

    def m_c2():                 # any session id is a plain name
        return patched(store, "SESSION_ID_RE", re.compile(r"(?s).*"))

    def m_c3():                 # the previous copy is not read, the record is believed
        return mutate_source(store.copy_version, [("good, why = _verify_copy(src, lc)", "good, why = True, ''")])

    def m_c4():                 # the door ignores disableAllHooks
        real = m.door_state

        def blind(project=None):
            st = real(project)
            st["disabled"] = None
            return st
        return patched(m, "door_state", blind)

    def m_c5():                 # uninstall takes everything that ends with the hook path
        return patched(m, "is_exactly_ours", lambda c, recorded=None: m.is_ours(c))

    run_mutation(t, "C1 restore overwrites a file that appeared at the target", m_c1, [t_n12_c1_restore_race])
    run_mutation(t, "C2 any session id is a plain name", m_c2, [t_n12_c2_session_id])
    run_mutation(t, "C3 the previous copy is not read before `same`", m_c3, [t_n12_c3_corrupt_last_copy])
    run_mutation(t, "C4 the door ignores disableAllHooks", m_c4, [t_n12_c4_door_when_hooks_disabled])
    run_mutation(t, "C5 uninstall takes a look-alike line", m_c5, [t_n12_c5_uninstall_exact])


def t_mutations_n13(t):
    """exact install: `_iter_ours` by is_ours (the old test) must turn the test red."""
    m = install_mod()

    def m_i1():
        def loose(groups, script, recorded=None):
            for g in groups:
                if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                    for h in g["hooks"]:
                        if isinstance(h, dict) and m.is_ours(h.get("command"), script):
                            yield g, h
        return patched(m, "_iter_ours", loose)

    run_mutation(t, "N13-1 `_iter_ours` by is_ours (install counts a look-alike as installed)", m_i1, [t_n13_install_exact])


GROUPS = [("1 recognize", [t_write_targets, t_delete_cases, t_delete_paths]),
          ("2 store", [t_config, t_store_label, t_store_zone, t_trash_roundtrip, t_trash_names,
                       t_trash_index_failure, t_restore_no_overwrite, t_version_copy, t_version_verify,
                       t_restore_version, t_find_days, t_unicode_and_long, t_link, t_transcript]),
          ("3 pre_tool", [t_hook_edit, t_hook_deny_shape, t_hook_delete, t_hook_mentions, t_hook_write,
                         t_hook_zone, t_hook_no_copy_denies, t_hook_broken_store, t_hook_size_limit,
                         t_hook_ceilings, t_hook_git_rollback, t_hook_failures, t_session_hook]),
          ("4 install", [t_install]),
          ("5 mutations", [t_mutations]),
          ("6 fixes", [t_hook_utf8_stdin, t_hook_utf8_inproc, t_session_hook_utf8, t_session_sweep_without_current_file,
                       t_hook_recognizer_fault, t_hook_mask_ceiling, t_install_timeout_budget,
                       t_hook_store_write_denied, t_delete_pipeline_and_values, t_nested_shells, t_copy_recursive,
                       t_comments, t_git_no_head, t_git_diff_fails, t_index_parallel, t_index_lock_busy,
                       t_hook_long_paths, t_ready_command_quoting, t_cli_trash_exit_codes, t_install_check,
                       t_hook_multiedit, t_gitignore]),
          ("7 independent review", [t_review_junction_into_store, t_review_is_ours_boundary]),
          ("8 personal folder", [t_personal_layout, t_personal_permissions_read, t_personal_permissions_refused,
                                 t_personal_other_account, t_personal_private_false_and_status,
                                 t_personal_open_folder_is_named, t_personal_identity,
                                 t_personal_other_users_folder, t_personal_docs_and_version]),
          ("9 the agent guide", [t_n2_guide_at_session_start, t_n2_find_gives_what_an_agent_needs, t_n2_numbered_names,
                          t_n2_creation_report, t_n2_docs]),
          ("10 the personal store", [t_n3_created_closed, t_n3_parallel_start, t_n3_sid_from_token, t_n3_lazy_root_and_name, t_n3_volume_rights,
                           t_n3_extra_rights_stay_mine, t_n3_find_not_searched, t_n3_owner_unreadable,
                           t_n3_session_reasons, t_n3_system_account_and_leftovers, t_n3_docs]),
          ("11 removal and re-install", [t_n4_command_runs_in_git_bash, t_n4_form_boundaries, t_n4_dead_lines,
                           t_n4_old_form_upgrade_and_check, t_n4_agent_and_docs]),
          ("12 removal and re-install mutations", [t_mutations_n4]),
          ("13 the plugin plugin", [t_n5_files, t_n5_command_in_git_bash, t_n15_no_python, t_n15_install_upgrades_020, t_n5_two_doors_runtime, t_n5_installer_refuses,
                                  t_n5_doors_named, t_n5_docs]),
          ("14 the plugin mutations", [t_mutations_n5, t_mutations_n15]),
          ("15 proof instead of looks proof instead of looks", [t_n6_record_proof, t_n6_absence, t_n6_plugin_proof, t_n6_yield_proof, t_n6_config_dir,
                                                  t_n6_disable_all_hooks, t_n6_git_bash, t_n6_shared_file, t_n6_not_measured, t_n6_docs]),
          ("16 proof instead of looks mutations", [t_mutations_n6]),
          ("17 the stand findings stand findings", [t_n7_plugins_root, t_n7_run_folder, t_n7_subfolder_note, t_n7_docs]),
          ("18 the stand findings mutations", [t_mutations_n7]),
          ("19 the second plugin review, third round", [t_n8_plugins_root, t_n8_run_folder, t_n8_check_shapes, t_n8_subfolder_check, t_n8_docs]),
          ("20 the second plugin review mutations", [t_mutations_n8]),
          ("21 the staging findings staging findings", [t_n9_clone_copy, t_n9_label_retry, t_n9_race_twelve_hooks]),
          ("22 the staging findings mutations", [t_mutations_n9]),
          ("23 the creation lock creation lock", [t_n10_creation_lock]),
          ("24 the creation lock mutations", [t_mutations_n10]),
          ("25 the refusal note refusal note", [t_n11_refusal_note]),
          ("26 the refusal note mutations", [t_mutations_n11]),
          ("27 the independent-review findings", [t_n12_c1_restore_race, t_n12_c2_session_id, t_n12_c3_corrupt_last_copy,
                                           t_n12_c4_door_when_hooks_disabled, t_n12_c5_uninstall_exact]),
          ("28 the independent-review findings mutations", [t_mutations_n12]),
          ("29 exact install install exact", [t_n13_install_exact]),
          ("30 exact install mutations", [t_mutations_n13])]


def exit_code(counts):
    """1 when anything is red or crashed; 2 when something could NOT BE MEASURED (and nothing is red); else 0."""
    if counts.get("FAIL", 0) or counts.get("CRASH", 0):
        return 1
    return 2 if counts.get("NOT MEASURED", 0) else 0


def _install_needs_bash():
    """Without Git Bash `install` refuses (item 6 of order 6): a test that installs cannot be measured here. It says NOT MEASURED -- not
    red (nothing is wrong with the kit) and not green. (`find_bash` patched by a test itself is a different thing and is left alone.)"""
    m = install_mod()
    if m.find_bash():
        return
    real = m.install

    def refuse(path, add_entries=True):
        if add_entries:
            raise NotMeasured("Git Bash was not found: `install` refuses (order 6, item 6), so the test that installs was NOT measured")
        return real(path, add_entries)
    m.install = refuse


def main():
    total = T()
    os.environ.pop("CLAUDE_CODE_PLUGIN_CACHE_DIR", None)      # a variable of the machine the tests run on is not a condition of any test
    only = sys.argv[1:]
    _install_needs_bash()
    for name, fns in GROUPS:
        if only and name.split()[0] not in only:
            continue
        say("== %s ==" % name)
        for fn in fns:
            total.run(fn)
    counts = {}
    for st, _n, _d in total.rows:
        counts[st] = counts.get(st, 0) + 1
    say("== total: PASS %d / FAIL %d / CRASH %d / SKIP %d / NOT MEASURED %d ==" % (
        counts.get("PASS", 0), counts.get("FAIL", 0), counts.get("CRASH", 0), counts.get("SKIP", 0), counts.get("NOT MEASURED", 0)))
    for st, n, d in total.rows:
        if st == "NOT MEASURED":
            say("NOT MEASURED: %s -- %s" % (n, d))
    say("temporary folder (nothing was deleted): %s" % RUN)
    return exit_code(counts)


if __name__ == "__main__":
    sys.exit(main())
