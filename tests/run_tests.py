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
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
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


class Skip(Exception):
    pass


def say(text):
    """print() that cannot die on a character the console code page has no letter for."""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(enc, "backslashreplace").decode(enc))


class T(object):
    """Collects the results of one run of test functions. Statuses: PASS, FAIL (an assertion is red),
    CRASH (the test function itself raised: nothing was decided), SKIP."""

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
    """A store inside the sandbox + settings without the kit's own shedkeeper.ini."""
    root = os.path.join(sb, "store")
    with env(SHEDKEEPER_STORE_ROOT=root, SHEDKEEPER_INI=os.path.join(sb, "no_such.ini")):
        st = settings(**over)
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
        ok, why = store.ensure_store(root, st)
        t.check("store: a folder without a label but with content is refused", not ok, why)
        t.check("store: the refusal says how to accept it and how to rename",
                "adopt" in why and "store_dir_name" in why, why)
        t.check("store: nothing was written into the foreign folder", os.listdir(root) == ["someone_elses.txt"],
                os.listdir(root))
        okk, msg = store.adopt(root, st)
        t.check("store: adopt accepts it (a person's decision)", okk and store.label_state(root)[0] == "ours", msg)
        empty = os.path.join(sb, "vol2", "_shedkeeper")
        os.makedirs(empty)
        ok, note = store.ensure_store(empty, st)
        t.check("store: an empty unmarked folder is taken silently (label put)",
                ok and store.label_state(empty)[0] == "ours" and "taken" in note, (ok, note))
        bad = os.path.join(sb, "vol3", "_shedkeeper")
        os.makedirs(bad)
        write(os.path.join(bad, store.LABEL), "not json")
        ok, why = store.ensure_store(bad, st)
        t.check("store: an unreadable label is refused", not ok and "unreadable" in why, why)
        filed = os.path.join(sb, "vol4", "_shedkeeper")
        write(filed)
        ok, why = store.ensure_store(filed, st)
        t.check("store: a file in place of the folder is refused", not ok and "not a folder" in why, why)
        new = os.path.join(sb, "vol5", "_shedkeeper")
        real = store._drive_type
        try:
            store._drive_type = lambda r: 2          # removable
            ok, why = store.ensure_store(new, st)
            t.check("store: a removable volume -> refused, nothing created",
                    not ok and "removable" in why and not os.path.exists(new), (ok, why))
            store._drive_type = lambda r: 4          # network
            ok, why = store.ensure_store(new, st)
            t.check("store: a network volume -> refused", not ok and "network" in why, why)
            store._drive_type = lambda r: 3          # fixed
            ok, note = store.ensure_store(new, settings(auto_create_store=False))
            t.check("store: auto_create_store = false -> refused, nothing created",
                    not ok and not os.path.exists(new), (ok, note))
            ok, note = store.ensure_store(new, st)
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
        t.check("trash: the label was created with the store", store.label_state(root)[0] == "ours")
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


def hook_sub(script, data, raw=None):
    """Run a hook the way Claude Code does: a subprocess in a CLEANED environment, the JSON on stdin as
    raw UTF-8 BYTES (not escaped to ASCII). `raw` (bytes or str) replaces the JSON. -> (rc, stdout, stderr)."""
    if raw is None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    elif isinstance(raw, str):
        raw = raw.encode("utf-8")
    p = subprocess.run([PY, script], input=raw, capture_output=True, timeout=60, env=clean_env())
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
                log = lines(os.path.join(sb, "store_allow", "log.jsonl"))
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
        t.check("session: with the hooks installed and a usable store SessionStart is silent", rc_i == 0 and rc == 0 and out.strip() == "", (rc_i, out))
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


def inst(*args):
    p = subprocess.run([PY, INSTALL] + list(args), capture_output=True, text=True, encoding="utf-8",
                       timeout=120, env=clean_env())
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
        t.check("install: the command is python \"<absolute kit path>\\hooks\\pre_tool.py\"",
                pre[3] == 'python "%s"' % os.path.join(KIT, "hooks", "pre_tool.py"), pre[3])
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
# 6. the fixes after the lenses (2026-09-30): each test here is red on the kit as it was before
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
    sid = "\u0441\u0438\u0434-\u0418"
    tp = write(os.path.join(sb, "cc", "projects", folder, "\u0441\u0435\u0441\u0441\u0438\u044f.jsonl"), "1\n2\n")
    proj = os.path.join(sb, "\u043f\u0440\u043e\u0435\u043a\u0442")
    os.makedirs(proj)
    with store_env(sb) as (root, st), env(SHEDKEEPER_USER_HOME=home):
        ev = {"hook_event_name": "SessionEnd", "transcript_path": tp, "session_id": sid, "cwd": proj}
        rc, out, err = hook_sub(SESSION, ev)
        dst = os.path.join(root, "transcripts", folder, sid + ".jsonl")
        t.check("session utf-8: a transcript with a non-ASCII path and session id is saved",
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

        broken = cloned()
        for groups in broken["hooks"].values():
            for g in groups:
                for h in g["hooks"]:
                    h["command"] = h["command"].replace("python ", "no_such_python_zz ", 1)
        write(sp, json.dumps(broken))
        rc, out = inst("check", "--project", proj)
        t.check("check: hooks whose command does not start -> exit 1, the probe FAILED (was: 'python in PATH' and exit 0)",
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

    # --- the guards of the act after the lenses
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
          ("7 independent review", [t_review_junction_into_store, t_review_is_ours_boundary])]


def main():
    total = T()
    only = sys.argv[1:]
    for name, fns in GROUPS:
        if only and name.split()[0] not in only:
            continue
        say("== %s ==" % name)
        for fn in fns:
            total.run(fn)
    counts = {}
    for st, _n, _d in total.rows:
        counts[st] = counts.get(st, 0) + 1
    say("== total: PASS %d / FAIL %d / CRASH %d / SKIP %d ==" % (counts.get("PASS", 0), counts.get("FAIL", 0),
                                                                  counts.get("CRASH", 0), counts.get("SKIP", 0)))
    say("temporary folder (nothing was deleted): %s" % RUN)
    return 1 if counts.get("FAIL", 0) or counts.get("CRASH", 0) else 0


if __name__ == "__main__":
    sys.exit(main())
