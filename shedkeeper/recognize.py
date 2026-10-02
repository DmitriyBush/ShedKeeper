"""recognize.py -- recognizers over the TEXT of a shell command (Bash or PowerShell).

Two questions, both answered from the command text alone:

  write_targets(cmd, cwd, shell)   which existing files may this command OVERWRITE?
  delete_targets(cmd, cwd, shell)  does this command DELETE, and which paths?

An error towards "one target too many" costs one extra copy; a missed target is a declared border:
the hook denies nothing it cannot see.

--- write_targets ---------------------------------------------------------------------------
Forms (a literal path in the command text):
  redirect   `>`, `>|`, `N>`, `&>` outside quotes (not `>>`, not `>&N`, not /dev/null)
  tee        `tee FILE...` without -a
  sed -i     every file argument (masks `*` are expanded, up to MAX_GLOB)
  cp / mv    the destination (an existing folder as destination: folder/name of the source); install
  ps         Set-Content / Out-File / Copy-Item / Move-Item and their aliases (sc, copy, cpi, move, mi)
  download   curl -o/--output, wget -O
  python     Path('x').write_text/write_bytes, open('x', 'w'|'x'...), shutil.copy*/move(_, 'x')
  git        checkout -- / checkout <ref> <path> / checkout . / checkout -f / restore (not only
             --staged) / reset --hard / switch -f -> the list of rollback folders (with -C and cd)
Taken into account: `cd`/`pushd`/`Set-Location` along the command (and in `( ... )`), `VAR=x` before
the verb, a line continued with `\\` or a backtick, heredoc bodies (data, except python writes).
The redirection of a segment that is itself a WRAPPER (`bash -c "..." > f`, `pwsh -Command "..." >> f`) is read like any other (`_outer_redirects`).
NOT seen (declared): writes inside programs the command runs, paths from variables ($X, %X%),
computed paths -- except for the guard of files that must not be written at all, which asks `unreadable_writes`: the writes (`>`, `>>`, tee, sed -i, cp / mv,
Add-Content ...) whose target is not a literal path; the hook refuses them when the target or the command points at the settings of the kit or the record of the
conversation.

--- delete_targets --------------------------------------------------------------------------
The HEAD of an executed command is judged, not a word anywhere in the line: `grep rm file`,
`echo "rm -rf /"`, `git commit -m "remove rm"`, `cat del.txt` are mentions, not deletions.
Executed places: segments of the line (`;` `&&` `||` `|` newline); `$( )`, backticks; the body of a
heredoc or here-string that a SHELL or PYTHON reads; the string after `-c` / `-Command` of a shell and
the rest of the line after `cmd /c`; the code of `python -c`; the command after `find -exec`; `{ }`
blocks. Deleting heads: rm rmdir del erase rd unlink shred Remove-Item ri, `git clean -f`,
`git rm` (not --cached), `find -delete`, `find -exec rm`, robocopy /MIR /PURGE /MOV /MOVE.
NOT seen (declared): deletion inside a script FILE (`python script.py`), a command assembled at run
time (variable, non-literal eval), other languages. A line the scanner cannot parse, or one that is
too long or too deeply nested, is judged coarsely (stricter, never softer) and reported unresolved.
"""
import ast
import glob as _glob
import os
import re
import shlex

NULLS = {'/dev/null', 'nul', 'NUL', '$null', '/dev/stdout', '/dev/stderr'}
MAX_GLOB = 200          # ceiling of a mask expansion per argument
MAX_NEST_DEPTH = 3      # nested code (bash -c, $( ), cmd /c ...)
MAX_PARSE_LEN = 50000   # above this the scanner is not run; the coarse judgement is used

# a mode that REWRITES the file: w, x (also wb, w+ ...) and r+ (r+b, rb+, rt+): `open('f', 'w')`, `open('f', mode='w')`, `Path('f').open('w')`. A mode that APPENDS
# (a, ab, a+ ...) overwrites nothing: it is read as `>>` (PY_APPEND, `append_targets`), not as a write that needs a copy first.
_PY_MODE = r"""r?['\"](?:[wx][bt+]*|r[bt]*\+[bt+]*)['\"]"""
_PY_MODE_APPEND = r"""r?['\"]a[bt+]*['\"]"""
PY_APPEND = re.compile(
    r"open\(\s*r?(['\"])(?P<b>[^'\"]+)\1\s*,\s*(?:mode\s*=\s*)?" + _PY_MODE_APPEND +
    r"|Path\(\s*r?(['\"])(?P<d>[^'\"]+)\3\s*\)\s*\.open\(\s*(?:mode\s*=\s*)?" + _PY_MODE_APPEND)
PY_WRITE = re.compile(
    r"Path\(\s*r?(['\"])(?P<a>[^'\"]+)\1\s*\)\s*\.write_(?:text|bytes)"
    r"|open\(\s*r?(['\"])(?P<b>[^'\"]+)\3\s*,\s*(?:mode\s*=\s*)?" + _PY_MODE +
    r"|shutil\.(?:copy|copy2|copyfile|move)\([^,()]+,\s*r?(['\"])(?P<c>[^'\"]+)\5"
    r"|Path\(\s*r?(['\"])(?P<d>[^'\"]+)\7\s*\)\s*\.open\(\s*(?:mode\s*=\s*)?" + _PY_MODE)
# heredoc marker: `<<` or `<<-`, NOT `<<<` (here-string)
HEREDOC = re.compile(r"(?<!<)<<(-?)(?!<)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
PS_ALIASES = {'sc': 'set-content', 'copy': 'copy-item', 'cpi': 'copy-item', 'move': 'move-item',
              'mi': 'move-item', 'mv': 'move-item', 'cp': 'copy-item'}
# PowerShell keys WITHOUT a value (otherwise they would "eat" the next positional path)
PS_SWITCHES = {'-force', '-recurse', '-nonewline', '-append', '-passthru', '-confirm', '-whatif',
               '-noclobber', '-asbytestream', '-container', '-notypeinformation', '-usebasicparsing'}
PREFIXES = {'do', 'then', 'else', '!', 'time', 'sudo', 'nohup', 'env', 'command', 'exec', 'xargs'}


def _strip_heredoc_bodies(cmd):
    """Return (command part, heredoc bodies). A body is the lines after the marker line up to the
    terminator line. A marker without a terminator is not a heredoc (e.g. `x << SHIFT` in code)."""
    lines = cmd.split('\n')
    keep, bodies, i = [], [], 0
    while i < len(lines):
        line = lines[i]
        keep.append(line)
        markers = [(m.group(1) == '-', m.group(3)) for m in HEREDOC.finditer(line)]
        i += 1
        for dash, word in markers:
            j = i
            while j < len(lines) and (lines[j].lstrip('\t') if dash else lines[j]).strip() != word:
                j += 1
            if j >= len(lines):
                continue            # no terminator -- not a heredoc
            bodies.append('\n'.join(lines[i:j]))
            i = j + 1               # the terminator line is not a command either
    return '\n'.join(keep), bodies


def _split_segments(cmd, ps=False, with_sep=False):
    """Cut by ; && || | and newline OUTSIDE quotes. `>|` is not a separator. With ps=True a backtick
    escapes the next character (PowerShell), and a backslash does not. With with_sep=True the result is
    [(segment, separator that stood before it)] -- '' for the first one."""
    segs, cur, q, i, sep = [], [], None, 0, ''
    while i < len(cmd):
        ch = cmd[i]
        if q:
            cur.append(ch)
            if ch == q:
                q = None
            elif ch == ('`' if ps else '\\') and q == '"' and i + 1 < len(cmd):
                cur.append(cmd[i + 1])
                i += 1
        elif ps and ch == '`' and i + 1 < len(cmd):
            cur.append(ch)
            cur.append(cmd[i + 1])
            i += 1
        elif ch in ('"', "'"):
            q = ch
            cur.append(ch)
        elif ch in ';\n|&':
            two = cmd[i:i + 2]
            if ch == '&' and two != '&&' and (ps or two == '&>' or (cur and cur[-1] == '>')):
                cur.append(ch)          # `&>`, `>&`, `2>&1` redirects; in PowerShell `&` is the call operator
            elif ch == '|' and cur and cur[-1] == '>':
                cur.append(ch)          # `>|` -- redirect with overwrite, not a pipe
            else:
                segs.append((''.join(cur), sep))
                cur = []
                sep = two if two in ('&&', '||') else ch
                if two in ('&&', '||'):
                    i += 1
        else:
            cur.append(ch)
        i += 1
    segs.append((''.join(cur), sep))
    out = [(x.strip(), sp) for x, sp in segs if x.strip()]
    return out if with_sep else [x for x, _sp in out]


def _strip_comments_lines(lines, ps=False):
    """Cut the `# ...` comments off a list of lines. A `#` starts a comment only OUTSIDE quotes and at
    the start of a word (`a#b`, `$#`, `${x#y}`, `http://h/#f` are not comments). The quote state runs
    on from one line to the next, as in the shell."""
    out, q = [], None
    for line in lines:
        buf, i, n = [], 0, len(line)
        while i < n:
            ch = line[i]
            if q:
                buf.append(ch)
                if ch == q:
                    q = None
                elif ch == ('`' if ps else '\\') and q == '"' and i + 1 < n:
                    buf.append(line[i + 1])
                    i += 1
            elif ch in ('\\', '`') and (ch == '`') == ps and i + 1 < n:
                buf.append(ch)
                buf.append(line[i + 1])
                i += 1
            elif ch in ('"', "'"):
                q = ch
                buf.append(ch)
            elif ch == '#' and (i == 0 or line[i - 1] in ' \t;&|('):
                break
            else:
                buf.append(ch)
            i += 1
        out.append(''.join(buf))
    return out


def _redirect_targets(seg):
    """Targets of `>` outside quotes in one segment."""
    out, q, i, n = [], None, 0, len(seg)
    while i < n:
        ch = seg[i]
        if q:
            if ch == q:
                q = None
            elif ch == '\\' and q == '"':
                i += 1
            i += 1
            continue
        if ch in ('"', "'"):
            q = ch
            i += 1
            continue
        if ch == '>':
            prev = seg[i - 1] if i else ''
            nxt = seg[i + 1] if i + 1 < n else ''
            if prev == '>' or nxt == '>':          # >> (append) -- not an overwrite
                i += 2 if nxt == '>' else 1
                continue
            if nxt == '&':                           # >&2
                i += 2
                continue
            j = i + 1
            if nxt == '|':
                j += 1
            while j < n and seg[j] == ' ':
                j += 1
            if j < n and seg[j] in ('"', "'"):
                k = seg.find(seg[j], j + 1)
                tgt = seg[j + 1:k if k > 0 else n]
                i = (k + 1) if k > 0 else n
            else:
                k = j
                while k < n and seg[k] not in ' \t;|&)<>':
                    k += 1
                tgt = seg[j:k]
                i = k
            if tgt and prev != '<':
                out.append(tgt)
            continue
        i += 1
    return out


def _outer_redirects(seg, append=False):
    """The targets of the redirections that stand in the segment itself, outside quotes: `>` (and `>|`), or with append=True `>>`. A wrapper
    (`bash -c "..." >> f`, `pwsh -Command "..." > f`) is a segment like any other: its own redirection is outside the string that is read inside."""
    return _append_redirect_targets(seg) if append else _redirect_targets(seg)


def _tokens(seg, ps=False):
    # a backslash before a letter/digit/dot/mask character is a Windows path separator, not an escape
    # (otherwise shlex eats it: C:\a\x.txt -> C:ax.txt, C:\new\* -> C:new*); in PowerShell a backslash
    # before a space or at the end is one too (`Copy-Item a.txt C:\proj\ -Force`)
    s = re.sub(r'\\(?=[A-Za-z0-9_.\-*?]|\s|$)' if ps else r'\\(?=[A-Za-z0-9_.\-*?])', '/', seg)
    try:
        return shlex.split(s, posix=True)
    except ValueError:
        return s.split()


def _is_literal(p):
    return bool(p) and not re.search(r'[$%`*?{}\[\]<>|]', p) and p not in NULLS


def _expand(p, cwd, limit=MAX_GLOB, notes=None):
    """Literal path -> [path]; a mask with * or ? -> expanded paths; else []. A mask that names more
    than `limit` paths returns limit + 1 of them AND adds a line to `notes`: the caller must see that
    not every path was named (a silent cut at the ceiling would defeat the ceiling)."""
    if _is_literal(p):
        return [p]
    if re.search(r'[$%`{}<>|]', p) or p in NULLS or not re.search(r'[*?]', p):
        return []
    base = to_abs(p, cwd)
    try:
        hits = sorted(_glob.glob(base))
    except Exception:
        return []
    if len(hits) > limit:
        if notes is not None:
            notes.append("the mask %s names more than %d paths" % (p, limit))
        hits = hits[:limit + 1]
    return hits


def to_abs(p, cwd):
    """A path from command text -> an absolute Windows path (forward slashes)."""
    p = p.strip().strip("\"'").replace('\\', '/')
    if p.startswith('~'):
        p = os.path.expanduser('~').replace('\\', '/') + p[1:]
    m = re.match(r'^/([a-zA-Z])(/.*)?$', p)             # /c/x -> C:/x (Git Bash)
    if m:
        p = m.group(1).upper() + ':' + (m.group(2) or '/')
    if not re.match(r'^[A-Za-z]:/', p):
        p = (cwd or '').replace('\\', '/').rstrip('/') + '/' + p
    return os.path.normpath(p).replace('\\', '/')


def _git_rollback_dir(toks, cwd):
    """If the segment is a git rollback of the working tree, return the repository folder (with -C);
    else None."""
    if not toks or os.path.basename(toks[0]).lower() not in ('git', 'git.exe'):
        return None
    i, gdir = 1, cwd
    while i < len(toks) and toks[i].startswith('-'):
        if toks[i] == '-C' and i + 1 < len(toks):
            gdir = to_abs(toks[i + 1], cwd)
            i += 2
            continue
        if toks[i] == '-c' and i + 1 < len(toks):
            i += 2
            continue
        i += 1
    if i >= len(toks):
        return None
    sub, args = toks[i], toks[i + 1:]
    opts = {a for a in args if a.startswith('-')}
    pos = [a for a in args if not a.startswith('-')]
    hit = False
    if sub == 'checkout':
        if not (opts & {'-b', '-B', '--orphan'}):
            hit = ('--' in args or bool(opts & {'-f', '--force'}) or '.' in pos or len(pos) >= 2)
    elif sub == 'restore':
        staged_only = bool(opts & {'--staged', '-S'}) and not (opts & {'--worktree', '-W'})
        hit = not staged_only
    elif sub == 'reset':
        hit = '--hard' in opts
    elif sub == 'switch':
        hit = bool(opts & {'-f', '--force', '--discard-changes'})
    return gdir if hit else None


WALK_CAP = 20000        # files compared while walking one source folder of a recursive copy


def _copy_targets(form, srcs, dest, cwd, isdir, recursive, max_files, notes):
    """Existing files that copying/moving `srcs` to `dest` would overwrite -> [(form, absolute path)].
    A file source: `dest` itself, or folder/name when `dest` is an existing folder. A folder source (only
    when `recursive`): every file below it that already exists at the same place under the destination
    folder. What cannot be listed is added to `notes` (the caller refuses on a note)."""
    out = []
    if not _is_literal(dest):
        return out
    dest_abs = to_abs(dest, cwd)
    into_dir = dest.endswith(('/', '\\')) or isdir(dest_abs)
    for s in srcs:
        names = _expand(s, cwd, max_files, notes)
        if not names and recursive and re.search(r'[$%`{}<>|]', s):
            notes.append("a recursive copy or move from a source that cannot be read from the text: %s" % s)
        for sp in names:
            sp_abs = to_abs(sp, cwd)
            if recursive and isdir(sp_abs):
                if not into_dir:
                    continue                    # the destination does not exist yet: nothing to overwrite
                contents = sp.endswith(('/.', '\\.'))
                base = dest_abs if contents else dest_abs.rstrip('/') + '/' + os.path.basename(sp_abs.rstrip('/'))
                walked = 0
                for dp, _dns, fns in os.walk(sp_abs):
                    rel = os.path.relpath(dp, sp_abs)
                    for fn in fns:
                        walked += 1
                        if walked > WALK_CAP:
                            notes.append("the folder %s holds more than %d files: the files it would overwrite "
                                         "were not listed" % (sp, WALK_CAP))
                            return out
                        tgt = os.path.normpath(os.path.join(base, rel, fn)).replace('\\', '/')
                        if os.path.isfile(tgt):
                            out.append((form, tgt))
                            if len(out) > max_files:
                                notes.append("a recursive copy or move from %s overwrites more than %d files"
                                             % (sp, max_files))
                                return out
            else:
                tgt = (dest.rstrip('/\\') + '/' + os.path.basename(sp.rstrip('/\\'))) if into_dir else dest
                out.append((form, tgt))
    return out


def _strip_to_head(toks):
    """The words of a segment from the REAL command on: after assignments, keywords and wrappers WITH their flags and values (`timeout 5`, `nice -n 5`,
    `sudo -u USER`, `xargs -n 1` ... the table of delete_targets: WRAPPERS, WRAPPER_VALUE_FLAGS). [] when there is no command."""
    i = _peel(toks)          # (assignments, do/then/else/!, time/sudo/nohup/env/command/exec/xargs and the rest of WRAPPERS: all of PREFIXES is peeled there, with the flags)
    return toks[i:] if i > 0 else (toks if i == 0 else [])


def write_targets(cmd, cwd, shell='bash', isdir=os.path.isdir, max_files=MAX_GLOB, notes=None):
    """([(form, absolute path)], [git rollback folders]). `isdir` is replaceable in tests.
    `max_files` is the ceiling of one hook run: a mask (or a recursive copy) that names more than that
    returns max_files + 1 paths and adds a line to `notes` (a list given by the caller); `notes` also
    receives a line for a recursive copy whose source cannot be read from the text."""
    if notes is None:
        notes = []
    if not isinstance(cmd, str) or not cmd.strip():
        return [], []
    ps = shell == 'powershell'
    res = []          # (form, path as in the text or already absolute, base folder)
    rollbacks = []
    text = cmd.replace('\\\n', ' ').replace('`\n', ' ')    # line continuation (bash / PowerShell)
    head, bodies = _strip_heredoc_bodies(text)
    head = '\n'.join(_strip_comments_lines(head.split('\n'), ps))
    for seg in _split_segments(head, ps):
        seg = seg.lstrip('({').rstrip(')}').strip() or seg
        toks = _tokens(seg, ps)
        # prefixes with the real verb behind them: VAR=x, do/then/else, sudo -u U, time, env, timeout 5, nice -n 5, xargs -n 1 ...
        toks = _strip_to_head(toks)
        verb = os.path.basename(toks[0]).lower() if toks else ''     # /bin/cp -> cp
        if verb.endswith('.exe'):
            verb = verb[:-4]
        # wrappers: bash -c "...", bash -lc "...", pwsh -Command "...", cmd /c "..." -> the same analysis
        # inside the string
        low = [t.lower() for t in toks]
        # the redirections of the segment ITSELF, also when the segment is a wrapper (`bash -c "..." > f`): they are outside the string
        for t in _outer_redirects(seg):
            if _is_literal(t):
                res.append(('redirect', t, cwd))
        if verb in ('bash', 'sh', 'zsh', 'dash'):
            k = next((k for k in range(1, len(toks) - 1)
                      if toks[k] == '-c' or _SH_C_CLUSTER.fullmatch(toks[k])), None)
            if k is not None:
                sub, sub_rb = write_targets(toks[k + 1], cwd, 'bash', isdir, max_files, notes)
                res += [(f, p, cwd) for f, p in sub]
                rollbacks += sub_rb
                continue
        if verb in ('pwsh', 'powershell') and any(t in ('-command', '-c') for t in low[1:-1]):
            k = next(i for i, t in enumerate(low) if i and t in ('-command', '-c'))
            if k + 1 < len(toks):
                sub, sub_rb = write_targets(toks[k + 1], cwd, 'powershell', isdir, max_files, notes)
                res += [(f, p, cwd) for f, p in sub]
                rollbacks += sub_rb
            continue
        if verb == 'cmd':
            k = next((k for k in range(1, len(toks)) if low[k] in ('/c', '/k', '//c', '//k')), None)
            if k is not None and k + 1 < len(toks):
                inner = ' '.join(toks[k + 1:])
                sub, sub_rb = write_targets(inner, cwd, 'bash', isdir, max_files, notes)
                res += [(f, p, cwd) for f, p in sub]
                rollbacks += sub_rb
                continue
        if verb in ('cd', 'pushd', 'set-location', 'sl', 'chdir'):
            dest = [t for t in toks[1:] if not t.startswith('-') and t.lower() != '/d']
            if dest and _is_literal(dest[0]):
                cwd = to_abs(dest[0], cwd)
            continue
        if not toks:
            continue
        g = _git_rollback_dir(toks, cwd)
        if g:
            rollbacks.append(g)
            continue
        args = [t for t in toks[1:] if not t.startswith('-')]
        if verb == 'tee' and not any(t in ('-a', '--append') for t in toks[1:]):
            res += [('tee', p, cwd) for t in args for p in _expand(t, cwd, max_files, notes)]
        elif verb == 'sed' and any(re.match(r'^-[a-zA-Z]*i', t) or t.startswith('--in-place')
                                   for t in toks[1:]):
            has_e = any(t in ('-e', '-f') or t.startswith('--expression') for t in toks[1:])
            files = args if has_e else args[1:]
            res += [('sed -i', p, cwd) for t in files for p in _expand(t, cwd, max_files, notes)]
        elif verb == 'perl' and any(re.match(r'^-[a-zA-Z]*i', t) for t in toks[1:]):
            # perl -pi -e 'script' files...: the first non-option is the script when given via -e
            has_e = any(re.match(r'^-[a-zA-Z]*e$', t) for t in toks[1:])
            files = args[1:] if has_e else args
            res += [('perl -i', p, cwd) for t in files for p in _expand(t, cwd, max_files, notes)]
        elif verb == 'dd':
            res += [('dd', t[3:], cwd) for t in toks[1:] if t.startswith('of=') and _is_literal(t[3:])]
        elif verb in ('cp', 'mv', 'install') and shell != 'powershell' and len(args) >= 2:
            recursive = verb == 'mv' or any(
                t in ('--recursive', '--archive') or re.match(r'^-[a-zA-Z]*[rRa][a-zA-Z]*$', t) for t in toks[1:])
            res += [(f, p, cwd) for f, p in
                    _copy_targets(verb, args[:-1], args[-1], cwd, isdir, recursive, max_files, notes)]
        elif verb in ('curl', 'wget'):
            for k, t in enumerate(toks[1:-1], start=1):
                if (verb == 'curl' and t in ('-o', '--output')) or (verb == 'wget' and t == '-O'):
                    if _is_literal(toks[k + 1]):
                        res.append(('download', toks[k + 1], cwd))
        else:
            psverb = PS_ALIASES.get(verb, verb) if (shell == 'powershell' or verb in
                                                    ('sc', 'cpi', 'mi', 'copy', 'move')) else verb
            psverb = {'iwr': 'invoke-webrequest', 'curl': 'invoke-webrequest',
                      'wget': 'invoke-webrequest'}.get(psverb, psverb) if shell == 'powershell' else psverb
            if psverb in ('set-content', 'out-file', 'copy-item', 'move-item', 'tee-object',
                          'invoke-webrequest', 'export-csv'):
                named, rest = {}, []
                it = iter(toks[1:])
                for t in it:
                    tl = t.lower()
                    if tl.startswith('-') and tl not in PS_SWITCHES and ':' not in tl:
                        named[tl] = next(it, '')
                    elif not tl.startswith('-'):
                        rest.append(t)
                if psverb in ('copy-item', 'move-item'):
                    src_words = named.get('-literalpath') or named.get('-path')
                    if src_words is not None:
                        srcs = [x for x in src_words.split(',') if x]
                        d = named.get('-destination') or (rest[0] if rest else '')
                    else:
                        srcs = [x for x in (rest[0] if rest else '').split(',') if x]
                        d = named.get('-destination') or (rest[1] if len(rest) > 1 else '')
                    recursive = psverb == 'move-item' or '-recurse' in low[1:]
                    res += [(f, p, cwd) for f, p in
                            _copy_targets('ps', srcs, d, cwd, isdir, recursive, max_files, notes)]
                    continue
                if psverb == 'invoke-webrequest':
                    d = named.get('-outfile', '')
                elif psverb == 'tee-object':
                    d = named.get('-filepath') or named.get('-path') or named.get('-literalpath', '')
                else:
                    d = named.get('-path') or named.get('-filepath') or named.get('-literalpath') \
                        or (rest[0] if rest else '')
                if _is_literal(d):
                    res.append(('ps', d, cwd))
    # python writes: in the command part and in heredoc bodies; a relative path is from the cwd after cd
    for part in [head] + bodies:
        for m in PY_WRITE.finditer(part):
            p = m.group('a') or m.group('b') or m.group('c') or m.group('d')
            if _is_literal(p):
                res.append(('python', p, cwd))
    seen, out = set(), []
    for form, p, base in res:
        a = to_abs(p, base)
        if a not in seen:
            seen.add(a)
            out.append((form, a))
    return out, rollbacks


# =============================================================================================
# delete_targets
# =============================================================================================

DEL_HEADS = {'rm', 'del', 'erase', 'rmdir', 'rd', 'remove-item', 'ri', 'unlink', 'shred', 'srm'}
# nested "code" carriers and wrappers
SHELLS = {'sh', 'bash', 'zsh', 'dash'}
PYTHONS = re.compile(r'^(py|pythonw?)(3(\.\d+)?)?$')
KEYWORDS = {'do', 'then', 'else', 'elif', '{', '(', '!', 'if', 'while', 'until', '}', ')', 'done',
            'fi', 'in'}
WRAPPERS = {'sudo', 'env', 'nohup', 'time', 'timeout', 'xargs', 'command', 'exec', 'nice', 'ionice',
            'stdbuf', 'builtin'}
WRAPPER_VALUE_FLAGS = {'timeout': {'-s', '-k', '--signal', '--kill-after'},
                       'env': {'-u', '-C', '-S', '--unset', '--chdir', '--split-string'},
                       'exec': {'-a'}, 'nice': {'-n', '--adjustment'}, 'ionice': {'-c', '-n', '-p'},
                       'stdbuf': {'-i', '-o', '-e'}, 'sudo': {'-u', '-g', '-C', '-D'},
                       'xargs': {'-n', '-I', '-i', '-d', '-P', '-L', '-l', '-s', '-a', '-E', '-e'}}
SH_FLAGS_WITH_ARG = {'-o', '+o', '-O', '+O', '--rcfile', '--init-file'}
PS_ARG_FLAGS = {'-executionpolicy', '-ep', '-windowstyle', '-w', '-outputformat', '-of', '-inputformat',
                '-if', '-configurationname', '-workingdirectory', '-wd', '-version', '-v',
                '-psconsolefile', '-settingsfile', '-custompipename'}
# value-taking parameters of Remove-Item and the PowerShell common parameters: their value is not a path
PS_DEL_VALUE_FLAGS = {'-include', '-exclude', '-filter', '-credential', '-stream',
                      '-erroraction', '-ea', '-warningaction', '-wa', '-informationaction', '-ia',
                      '-progresspreference', '-errorvariable', '-ev', '-warningvariable', '-wv',
                      '-informationvariable', '-iv', '-outvariable', '-ov', '-outbuffer', '-ob',
                      '-pipelinevariable', '-pv'}
GIT_GLOBAL_WITH_ARG = {'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path',
                       '--super-prefix', '--config-env'}
ROBOCOPY_DEL = {'mir', 'purge', 'mov', 'move'}
PY_EXEC_ATTRS = {'system', 'popen', 'run', 'call', 'check_call', 'check_output', 'Popen',
                 'getoutput', 'getstatusoutput'}
PY_DEL_TEXT = re.compile(r'\brmtree\s*\(|\bos\.(?:remove|unlink|rmdir|removedirs)\s*\(|'
                         r'\.unlink\s*\(|\.rmdir\s*\(')
# coarse judgement: a word anywhere (used only for what the scanner cannot parse / too long / too deep)
WORD_DEL = re.compile(r'(?<![\w.-])(?:rm|del|erase|rd|rmdir|unlink|shred|srm|remove-item)(?![\w-])|'
                      r'\bgit\s+clean\b|(?<![\w-])-delete\b|/(?:mir|purge|mov|move)\b|rmtree|'
                      r'\bos\.(?:remove|unlink|rmdir|removedirs)\b', re.I)
_NAIVE_SPLIT = re.compile(r'\|\||&&|;|\||\n|(?<![>&\d])&(?![>&])')
_ASSIGN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\+?=')
_PS_DOTNET_DEL = re.compile(r'\[(?:system\.)?io\.(?:file|directory)\]::delete$')
_SH_C_CLUSTER = re.compile(r'^-[a-zA-Z]*c[a-zA-Z]*$')
_CMD_SWITCH = re.compile(r'^(/{1,2}[a-zA-Z?])+$')
_REDIR = re.compile(r'^\d*[<>]+&?\S*$')


class _Unparsed(Exception):
    pass


class Deletion(object):
    """Result of delete_targets: `paths` (absolute, literal, as given -- existing or not),
    `unresolved` (a deletion was recognized whose paths could not be read from the text),
    `coarse` (the judgement fell back to the coarse word search)."""

    def __init__(self):
        self.paths = []
        self.unresolved = False
        self.coarse = False

    @property
    def found(self):
        return bool(self.paths) or self.unresolved

    def __iter__(self):     # allows: paths, unresolved = delete_targets(...)
        yield list(self.paths)
        yield self.unresolved

    def __repr__(self):
        return 'Deletion(paths=%r, unresolved=%r, coarse=%r)' % (self.paths, self.unresolved, self.coarse)


def _scan(text, ps):
    """Replace `$( )`, backtick and `<( )` substitutions by a placeholder word; return
    (new text, [substitution texts]). Quote-aware: `'...'` hides everything, `"..."` does not hide
    substitutions. An unclosed substitution runs to the end of the text."""
    out, subs, i, n, q = [], [], 0, len(text), None
    while i < n:
        ch = text[i]
        if q == "'":
            out.append(ch)
            if ch == "'":
                q = None
            i += 1
            continue
        if ch == '\\' and not ps and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch == '`' and ps and i + 1 < n:      # PowerShell escape character
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch == '`' and not ps:
            j = text.find('`', i + 1)
            end = j if j > 0 else n
            subs.append(text[i + 1:end])
            out.append(' __SUB__ ')
            i = end + 1
            continue
        if ch == '$' and i + 1 < n and text[i + 1] == '(' or (
                ch in '<>' and i + 1 < n and text[i + 1] == '(' and q is None):
            start = i + 2
            depth, j, iq = 1, start, None
            while j < n and depth:
                c = text[j]
                if iq:
                    if c == iq:
                        iq = None
                elif c in ('"', "'"):
                    iq = c
                elif c == '(':
                    depth += 1
                elif c == ')':
                    depth -= 1
                j += 1
            end = j - 1 if depth == 0 else n
            subs.append(text[start:end])
            out.append(' __SUB__ ')
            i = j if depth == 0 else n
            continue
        if ch == '"':
            q = None if q == '"' else '"'
        elif ch == "'" and q is None:
            q = "'"
        out.append(ch)
        i += 1
    return ''.join(out), subs


def _brace_split(seg, ps):
    """Cut a segment at `(` `)` and at `{` `}` that stand as separate blocks, outside quotes. In
    PowerShell every brace outside `${` is a block edge (`%{rm $_}`); in bash only a standalone `{`/`}`
    word is (`${VAR}`, `{a,b}`, find's `{}` are not)."""
    parts, cur, q, i, n = [], [], None, 0, len(seg)
    while i < n:
        ch = seg[i]
        if q:
            cur.append(ch)
            if ch == q:
                q = None
            elif ch == ('`' if ps else '\\') and q == '"' and i + 1 < n:
                cur.append(seg[i + 1])
                i += 1
        elif ps and ch == '`' and i + 1 < n:
            cur.append(ch)
            cur.append(seg[i + 1])
            i += 1
        elif ch in ('"', "'"):
            q = ch
            cur.append(ch)
        elif ch == '$' and i + 1 < n and seg[i + 1] == '{':
            j = seg.find('}', i)
            j = n - 1 if j < 0 else j
            cur.append(seg[i:j + 1])
            i = j
        elif ch in '()':
            parts.append(''.join(cur))
            cur = []
        elif ch in '{}':
            prev = seg[i - 1] if i else ' '
            nxt = seg[i + 1] if i + 1 < n else ' '
            standalone = prev in ' \t;' and nxt in ' \t;'
            if ps or standalone:
                parts.append(''.join(cur))
                cur = []
            else:
                cur.append(ch)
        else:
            cur.append(ch)
        i += 1
    parts.append(''.join(cur))
    return [p.strip() for p in parts if p.strip()]


def _words(seg, ps=False):
    """Split a segment into words: quotes group, a backslash before a name character is a path
    separator (Windows), `\\x` before punctuation escapes it. In PowerShell (ps=True) the backtick is
    the escape character and a backslash never escapes a quote. No expansion is done."""
    words, cur, has, q, i, n = [], [], False, None, 0, len(seg)
    while i < n:
        ch = seg[i]
        if q == "'":
            if ch == "'":
                q = None
            else:
                cur.append(ch)
        elif q == '"':
            if ch == '"':
                q = None
            elif ps and ch == '`' and i + 1 < n:
                cur.append(seg[i + 1])
                i += 1
            elif not ps and ch == '\\' and i + 1 < n and seg[i + 1] in '"\\$`':
                cur.append(seg[i + 1])
                i += 1
            else:
                cur.append(ch)
        elif ch in ('"', "'"):
            q, has = ch, True
        elif ch in ' \t':
            if has or cur:
                words.append(''.join(cur))
            cur, has = [], False
        elif ps and ch == '`' and i + 1 < n:
            cur.append(seg[i + 1])
            has = True
            i += 1
        elif ch == '\\' and i + 1 < n:
            nx = seg[i + 1]
            if re.match(r'[A-Za-z0-9_.\-]', nx):
                cur.append('/')
            else:
                cur.append(nx)
                i += 1
            has = True
        else:
            cur.append(ch)
        i += 1
    if has or cur:
        words.append(''.join(cur))
    return words


def _strip_redirects(toks):
    out, skip = [], False
    for t in toks:
        if skip:
            skip = False
            continue
        if t == '<<<':                          # here-string: keep the marker AND its word
            out.append(t)
            continue
        if re.fullmatch(r'\d*[<>]+&?', t):      # `>` `2>` `>>` `<` -- the target is the next word
            skip = not t.endswith('&')
            continue
        if _REDIR.match(t):
            continue
        out.append(t)
    return out


_PATH_WITH_SPACES = re.compile(r'(?:[A-Za-z]:|~)?[\\/].*[\\/][^\s\\/]+')


def _basename(tok):
    if re.search(r'\s', tok):
        if _PATH_WITH_SPACES.fullmatch(tok) and not re.search(r'\s-', tok):
            tok = re.split(r'[\\/]', tok)[-1]    # a PATH with a space in it (`"C:\Program Files\...\psql.exe"`): the file name is the head
        else:
            return tok.lower()          # a whole command line in one word (`cmd /c "..."`): no head here
    b = re.split(r'[\\/]', tok)[-1].lower()
    if b.endswith('.exe'):
        b = b[:-4]
    return b


def _peel(toks, info=None):
    """Index of the real head in the words of a piece (after assignments, keywords, wrappers and
    their flags, PowerShell `$x =` and `[type]` casts); -1 -- no head. `info` (a dict) gets
    info['xargs'] = True when an `xargs` wrapper was peeled: the head then also acts on piped input."""
    i, wrapper, want_duration = 0, None, False
    while i < len(toks):
        t = toks[i]
        b = _basename(t)
        if _ASSIGN.match(t) or t in KEYWORDS:
            i += 1
            continue
        if t in ('&', '.') and i + 1 < len(toks):
            i += 1                      # PowerShell call operator `& "C:\Program Files\...\psql.exe" ...` and dot-sourcing `. psql ...`: the head is the next word
            continue
        if t.startswith('$') and i + 1 < len(toks) and toks[i + 1] == '=':
            i += 2                      # PowerShell `$null = Remove-Item x`
            continue
        if re.fullmatch(r'\[[\w.]+\]', t) and not _PS_DOTNET_DEL.fullmatch(t.lower()):
            i += 1                      # PowerShell cast `[void]`
            continue
        if b in WRAPPERS:
            wrapper, want_duration = b, b == 'timeout'
            if b == 'xargs' and info is not None:
                info['xargs'] = True
            i += 1
            continue
        if wrapper:
            if t in WRAPPER_VALUE_FLAGS.get(wrapper, ()):
                i += 2
                continue
            if t.startswith('-') and len(t) > 1:
                i += 1
                continue
            if want_duration and re.fullmatch(r'\d+(\.\d+)?[smhd]?', t):
                want_duration = False
                i += 1
                continue
            if wrapper in ('nice', 'ionice') and re.fullmatch(r'[-+]?\d+', t):
                i += 1
                continue
        return i
    return -1


def _switches(args):
    """cmd-style switches /S, //S, /S/Q (Git Bash doubles the slash) -> a set of lowercase letters."""
    out = set()
    for a in args:
        if re.fullmatch(r'(/{1,2}[a-zA-Z?][a-zA-Z0-9:]*)+', a):
            out.update(x for x in a.lower().replace('//', '/').split('/') if x)
    return out


def _git_sub(args):
    k = 0
    while k < len(args):
        a = args[k]
        if a in GIT_GLOBAL_WITH_ARG:
            k += 2
            continue
        if a.startswith('-'):
            k += 1
            continue
        return a.lower(), args[k + 1:]
    return '', []


def _path_args(head, args, tool):
    """Path words of a deleting command: (list of words, no_paths_flag)."""
    ps_like = head in ('remove-item', 'ri') or (tool == 'PowerShell' and head in
                                                 ('rm', 'del', 'erase', 'rmdir', 'rd'))
    cmd_like = head in ('del', 'erase', 'rd', 'rmdir')
    paths, i, opts_done = [], 0, False
    while i < len(args):
        a = args[i]
        if not opts_done and a == '--':
            opts_done = True
        elif not opts_done and ps_like and a.startswith('-') and len(a) > 1 and not a[1].isdigit():
            al = a.lower().split(':', 1)
            if al[0] in ('-path', '-literalpath', '-lp'):
                val = al[1] if len(al) > 1 else (args[i + 1] if i + 1 < len(args) else '')
                if len(al) == 1:
                    i += 1
                paths += [x for x in re.split(r',', val) if x]
            elif al[0] in PS_DEL_VALUE_FLAGS and len(al) == 1:
                i += 1
        elif not opts_done and not ps_like and a.startswith('-') and len(a) > 1:
            pass                        # a GNU-style flag
        elif cmd_like and not opts_done and _CMD_SWITCH.match(a):
            pass                        # cmd switch
        else:
            paths += [x for x in a.split(',') if x] if ps_like and ',' in a else [a]
        i += 1
    return paths


def _delete_verdict(head, args, tool):
    """None when this head with these args does not delete; else (kind, path words or None).
    `None` for the paths means: deletion recognized, paths not readable from the text."""
    # rmdir / rd without /s remove only an EMPTY folder -- still a removal: it goes to the trash too
    # (an independent review found `rmdir -p` can take parent folders with it).
    if head in DEL_HEADS:
        p = _path_args(head, args, tool)
        return ('del', p)
    if _PS_DOTNET_DEL.fullmatch(head):
        return ('del', None)    # [System.IO.File]::Delete("x") -- the path is behind a paren
    if head == 'robocopy' and _switches(args) & ROBOCOPY_DEL:
        return ('del', None)
    if head == 'git':
        sub, rest = _git_sub(args)
        if sub == 'clean':
            dry = any(a in ('-n', '--dry-run') or (a.startswith('-') and not a.startswith('--')
                                                    and 'n' in a) for a in rest)
            force = any(a == '--force' or (a.startswith('-') and not a.startswith('--') and 'f' in a)
                        for a in rest)
            if force and not dry:
                return ('del', None)
        if sub == 'rm' and '--cached' not in rest:
            p = [a for a in rest if not a.startswith('-')]
            return ('del', p)
    if head == 'find' and '-delete' in args:
        return ('del', None)
    return None


def _nested(head, args):
    """[(text, kind)]: strings the head executes as code. kind: shell | pwsh | python."""
    out = []
    if head == 'eval' and args:
        return [(' '.join(args), 'shell')]
    if head in ('iex', 'invoke-expression') and args:
        return [(' '.join(a for a in args if a.lower() != '-command'), 'pwsh')]
    if head in ('powershell', 'pwsh'):
        j = 0
        while j < len(args):
            a = args[j].lower()
            if a in ('-command', '-c', '/c', '//c'):
                return [(' '.join(args[j + 1:]), 'pwsh')] if j + 1 < len(args) else []
            if a in ('-file', '-f', '-encodedcommand', '-enc', '-ec', '-e'):
                return []       # a script file / encoded text -- not visible (declared)
            if a in PS_ARG_FLAGS:
                j += 2
                continue
            if a.startswith('-'):
                j += 1
                continue
            return [(' '.join(args[j:]), 'pwsh')]
        return []
    if head in SHELLS:
        j = 0
        while j < len(args):
            a = args[j]
            if a in SH_FLAGS_WITH_ARG:
                j += 2
                continue
            if a == '-c' or _SH_C_CLUSTER.fullmatch(a):
                return [(args[j + 1], 'shell')] if j + 1 < len(args) else []
            if not a.startswith(('-', '+')):
                return []       # a script file
            j += 1
        return []
    if head == 'cmd':
        for j, a in enumerate(args):
            if a.lower() in ('/c', '/k', '//c', '//k'):
                one = args[j + 1:]
                if len(one) == 1 and re.search(r'\s', one[0]):
                    return [(one[0], 'shell')]      # cmd /c "rd /s /q C:\x": the string IS the command line
                rest = []
                for t in args[j + 1:]:
                    t = re.sub(r'^//([a-zA-Z?])$', r'/\1', t)       # Git Bash spelling of a switch
                    rest.append('"%s"' % t if re.search(r'\s', t) else t)
                return [(' '.join(rest), 'shell')] if rest else []
        return []
    if PYTHONS.match(head):
        j = 0
        while j < len(args):
            a = args[j]
            if a == '-c':
                if j + 1 < len(args):
                    out.append((args[j + 1], 'python'))
                break
            if a in ('-W', '-X'):
                j += 2
                continue
            if not a.startswith(('-', '+')) or a == '-m':
                break           # a script file / module; the rest are ITS arguments
            j += 1
    return out


def _stdin_code(head, args):
    """True when a shell/python head with these args reads its CODE from standard input."""
    if head in SHELLS:
        return not [a for a in args if not a.startswith(('-', '+')) or a == '-']
    if PYTHONS.match(head):
        rest = [a for a in args if not a.startswith('-') or a == '-']
        return not rest or rest == ['-']
    return False


def _python_code(code, depth, acc, cwd):
    """Deletion found in LITERAL python code (text search) and commands passed to os.system /
    subprocess with a literal string or list argument."""
    if PY_DEL_TEXT.search(code):
        acc.unresolved = True
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError, TypeError):
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        f = node.func
        attr = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
        if attr not in PY_EXEC_ATTRS:
            continue
        a0 = node.args[0]
        if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
            _walk(a0.value, 'Bash', depth + 1, acc, cwd)
        elif isinstance(a0, (ast.List, ast.Tuple)) and a0.elts and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in a0.elts):
            line = ' '.join('"%s"' % e.value if re.search(r'\s', e.value) else e.value for e in a0.elts)
            _walk(line, 'Bash', depth + 1, acc, cwd)


def _coarse(command, tool, acc):
    """Coarse and never softer than the scanner: heads of pieces, plus a word anywhere. Used only for
    an unparsable / too long / too deep text."""
    acc.coarse = True
    for part in _NAIVE_SPLIT.split(command):
        toks = _words(part, tool == 'PowerShell')
        i = _peel(toks)
        if i >= 0 and _delete_verdict(_basename(toks[i]), toks[i + 1:], tool):
            acc.unresolved = True
    if WORD_DEL.search(command):
        acc.unresolved = True


def _apply_verdict(verdict, cwd, acc):
    kind, words = verdict
    if not words:
        acc.unresolved = True
        return
    for w in words:
        if _is_literal(w):
            acc.paths.append(to_abs(w, cwd))
            continue
        expanded, notes = [], []
        if re.search(r'[*?]', w) and not re.search(r'[$%`{}<>|]', w):
            expanded = _expand(w, cwd, MAX_GLOB, notes)
        if notes:
            acc.unresolved = True       # a mask cut at the ceiling: not every path is named
        if not expanded:
            acc.unresolved = True       # a variable, or a mask that matches nothing
        acc.paths.extend(to_abs(e, cwd) for e in expanded)


def _walk(command, tool, depth, acc, cwd):
    if not isinstance(command, str) or not command.strip():
        return
    if depth > MAX_NEST_DEPTH or len(command) > MAX_PARSE_LEN:
        _coarse(command, tool, acc)
        return
    ps = tool == 'PowerShell'
    if ps:
        command = re.sub(r'`(?=[A-Za-z0-9_./\\-])', '', command)
    text = command.replace('\\\n', ' ')
    if ps:
        text = text.replace('`\n', ' ')
    # heredoc bodies belong to the line with the marker: keep them attached to that line
    lines = text.split('\n')
    kept, i, line_bodies = [], 0, {}
    while i < len(lines):
        line = lines[i]
        idx = len(kept)
        kept.append(line)
        markers = [(m.group(1) == '-', m.group(3)) for m in HEREDOC.finditer(line)]
        i += 1
        for dash, word in markers:
            j = i
            while j < len(lines) and (lines[j].lstrip('\t') if dash else lines[j]).strip() != word:
                j += 1
            if j >= len(lines):
                continue
            line_bodies.setdefault(idx, []).append('\n'.join(lines[i:j]))
            i = j + 1
    kept = _strip_comments_lines(kept, ps)          # `# ...` outside quotes is not code
    cwd_sure = True                     # False after a `cd` whose target is not a literal path: the later relative paths are read against the OLD folder
    for idx, line in enumerate(kept):
        scanned, subs = _scan(line, ps)
        for s in subs:
            _walk(s, tool, depth + 1, acc, cwd)
        segs = _split_segments(scanned, ps, with_sep=True)
        line_has_heredoc = idx in line_bodies
        stdin_kind = None
        pieces_by_seg = []
        for seg, sep in segs:
            pieces = _brace_split(seg, ps)
            pieces_by_seg.append((pieces, sep))
        visit = getattr(acc, 'visit', None)     # an observer of the executed heads (db.py): same scanner, other question
        prev_text = ''
        for pieces, sep in pieces_by_seg:
            for piece in pieces:
                toks = _strip_redirects(_words(piece, ps))
                if ps:
                    m = re.match(r'^\$[\w:]+=(.*)$', toks[0]) if toks else None
                    if m:
                        toks = ([m.group(1)] if m.group(1) else []) + toks[1:]
                info = {}
                k = _peel(toks, info)
                if visit is not None:
                    visit(_basename(toks[k]) if k >= 0 else '', toks[k + 1:] if k >= 0 else [],
                          toks[:k] if k >= 0 else toks,
                          {'tool': tool, 'cwd': cwd, 'sep': sep, 'piece': piece, 'prev': prev_text,
                           'bodies': line_bodies.get(idx, []), 'cwd_sure': cwd_sure})
                if k < 0:
                    continue
                head, args = _basename(toks[k]), toks[k + 1:]
                if head in ('cd', 'pushd', 'set-location', 'sl', 'chdir'):
                    dest = [t for t in args if not t.startswith('-') and t.lower() != '/d']
                    if dest and _is_literal(dest[0]):
                        cwd = to_abs(dest[0], cwd)
                    else:
                        cwd_sure = False
                    continue
                v = _delete_verdict(head, args, tool)
                if v:
                    _apply_verdict(v, cwd, acc)
                    if sep == '|' or info.get('xargs'):
                        acc.unresolved = True   # the head also acts on piped input: not every path is in the text
                if head == 'find':
                    for j, a in enumerate(args):
                        if a in ('-exec', '-execdir', '-ok', '-okdir') and j + 1 < len(args):
                            sub = []
                            for t in args[j + 1:]:
                                if t in (';', '+') or t == '\\;':
                                    break
                                sub.append(t)
                            if sub and _delete_verdict(_basename(sub[0]), sub[1:], tool):
                                acc.unresolved = True
                for text2, kind in _nested(head, args):
                    if kind == 'python':
                        _python_code(text2, depth + 1, acc, cwd)
                    else:
                        _walk(text2, 'PowerShell' if kind == 'pwsh' else 'Bash', depth + 1, acc, cwd)
                if '<<<' in args:
                    j = args.index('<<<')
                    if j + 1 < len(args) and _stdin_code(head, [a for a in args[:j]]):
                        if head in SHELLS:
                            _walk(args[j + 1], 'Bash', depth + 1, acc, cwd)
                        else:
                            _python_code(args[j + 1], depth + 1, acc, cwd)
                if line_has_heredoc and stdin_kind is None and _stdin_code(head, args):
                    stdin_kind = 'shell' if head in SHELLS else 'python'
            prev_text = ' '.join(pieces)
        if line_has_heredoc and stdin_kind:
            for hb in line_bodies[idx]:
                if stdin_kind == 'shell':
                    _walk(hb, 'Bash', depth + 1, acc, cwd)
                else:
                    _python_code(hb, depth + 1, acc, cwd)


def delete_targets(command, cwd, shell='bash'):
    """Judge a Bash/PowerShell command text. Returns a Deletion: `.paths` (absolute literal paths the
    command deletes), `.unresolved` (a deletion whose paths cannot be read from the text: variables,
    masks without matches, nested or piped input), `.found` (either). Iterating gives (paths,
    unresolved). A text the scanner cannot parse (recursion, unbalanced input) is judged coarsely; any
    OTHER exception is a fault of this module and propagates -- the hook then applies its own last-resort
    check (pre_tool.rough_destructive) instead of assuming that the command is harmless."""
    acc = Deletion()
    tool = 'PowerShell' if str(shell).lower() in ('powershell', 'pwsh', 'ps') else 'Bash'
    if not isinstance(command, str) or not command.strip():
        return acc
    try:
        _walk(command, tool, 0, acc, cwd or os.getcwd())
    except (RecursionError, _Unparsed, ValueError, IndexError):
        _coarse(command, tool, acc)
    seen, uniq = set(), []
    for p in acc.paths:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    acc.paths = uniq
    return acc


# ---- appends: what write_targets leaves out because an append overwrites nothing (a guard of a FILE that must not be written at all asks for them)

def _append_redirect_targets(seg):
    """Targets of `>>` outside quotes in one segment."""
    out, q, i, n = [], None, 0, len(seg)
    while i < n:
        ch = seg[i]
        if q:
            if ch == q:
                q = None
            elif ch == '\\' and q == '"':
                i += 1
            i += 1
            continue
        if ch in ('"', "'"):
            q = ch
            i += 1
            continue
        if ch == '>' and seg[i + 1:i + 2] == '>' and seg[i - 1:i] != '>':
            j = i + 2
            while j < n and seg[j] == ' ':
                j += 1
            if j < n and seg[j] in ('"', "'"):
                k = seg.find(seg[j], j + 1)
                tgt = seg[j + 1:k if k > 0 else n]
                i = (k + 1) if k > 0 else n
            else:
                k = j
                while k < n and seg[k] not in ' \t;|&)<>':
                    k += 1
                tgt = seg[j:k]
                i = k
            if tgt:
                out.append(tgt)
            continue
        i += 1
    return out


def append_targets(cmd, cwd, shell='bash'):
    """[absolute path] of the files a command APPENDS to with a literal path: `>>`, `tee -a`, Add-Content, and python `open('f', 'a')` / `Path('f').open('a')`.
    `write_targets` does not list them (an append overwrites nothing, so nothing is copied first); the guard of the settings files and of the journals of
    the store, which must not be written at all, asks for them."""
    ps = shell == 'powershell'
    out = []
    if not isinstance(cmd, str) or not cmd.strip():
        return out
    text = cmd.replace('\\\n', ' ').replace('`\n', ' ')
    head, bodies = _strip_heredoc_bodies(text)
    head = '\n'.join(_strip_comments_lines(head.split('\n'), ps))
    for part in [head] + bodies:             # python: in the command part and in heredoc bodies, like write_targets
        for m in PY_APPEND.finditer(part):
            p = m.group('b') or m.group('d')
            if _is_literal(p):
                out.append(to_abs(p, cwd))
    for seg in _split_segments(head, ps):
        seg = seg.lstrip('({').rstrip(')}').strip() or seg
        toks = _strip_to_head(_tokens(seg, ps))
        verb = os.path.basename(toks[0]).lower() if toks else ''
        if verb.endswith('.exe'):
            verb = verb[:-4]
        low = [t.lower() for t in toks]
        for t in _outer_redirects(seg, append=True):         # (the segment's own `>>`, also when it is a wrapper: `bash -c "..." >> f`)
            if _is_literal(t):
                out.append(to_abs(t, cwd))
        if verb in ('bash', 'sh', 'zsh', 'dash'):
            k = next((k for k in range(1, len(toks) - 1) if toks[k] == '-c' or _SH_C_CLUSTER.fullmatch(toks[k])), None)
            if k is not None:
                out += append_targets(toks[k + 1], cwd, 'bash')
                continue
        if verb in ('pwsh', 'powershell') and any(t in ('-command', '-c') for t in low[1:-1]):
            k = next(i for i, t in enumerate(low) if i and t in ('-command', '-c'))
            if k + 1 < len(toks):
                out += append_targets(toks[k + 1], cwd, 'powershell')
            continue
        if verb == 'cmd':
            k = next((k for k in range(1, len(toks)) if low[k] in ('/c', '/k', '//c', '//k')), None)
            if k is not None and k + 1 < len(toks):
                out += append_targets(' '.join(toks[k + 1:]), cwd, 'bash')
                continue
        if verb in ('cd', 'pushd', 'set-location', 'sl', 'chdir'):
            dest = [t for t in toks[1:] if not t.startswith('-') and t.lower() != '/d']
            if dest and _is_literal(dest[0]):
                cwd = to_abs(dest[0], cwd)
            continue
        args = [t for t in toks[1:] if not t.startswith('-')]
        if verb == 'tee' and any(t in ('-a', '--append') for t in toks[1:]):
            out += [to_abs(t, cwd) for t in args if _is_literal(t)]
        elif verb in ('add-content', 'ac') and args and _is_literal(args[0]):
            out.append(to_abs(args[0], cwd))
    return list(dict.fromkeys(out))


# ---- writes whose target cannot be read from the text: the guard of a FILE that must not be written asks for them

PS_WRITE_VERBS = {'set-content': 'ps', 'add-content': 'ps', 'ac': 'ps', 'sc': 'ps', 'out-file': 'ps', 'tee-object': 'ps', 'export-csv': 'ps'}


def _opaque(t):
    """A write target that is not a literal path: a variable, a substitution, a mask, a computed name (`/dev/null`, `$null` and the like are no file)."""
    return bool(t) and t not in NULLS and not t.startswith('&') and not _is_literal(t)


def unreadable_writes(cmd, cwd, shell='bash'):
    """[(form, target text)] of the writes of a command whose target is NOT a literal path -- `>` / `>>` (also of a wrapper: `bash -c "..." >> $F`), `tee`,
    `sed -i`, `cp` / `mv` / `install` (the destination), `dd of=`, `Add-Content` / `Set-Content` / `Out-File` / `Tee-Object` / `Export-Csv` -- also inside `bash -c`,
    `pwsh -Command` and `cmd /c`. `write_targets` and `append_targets` cannot name the file of such a write, so a guard of files that must not be written at all
    (the settings of the kit, the record of the conversation) cannot tell where it goes; the hook asks this function, and refuses such a write when the target or the
    text of the command points at what is guarded. A write from inside a program the command runs stays outside (declared)."""
    ps = shell == 'powershell'
    out = []
    if not isinstance(cmd, str) or not cmd.strip():
        return out
    text = cmd.replace('\\\n', ' ').replace('`\n', ' ')
    head, _bodies = _strip_heredoc_bodies(text)
    head = '\n'.join(_strip_comments_lines(head.split('\n'), ps))
    for seg in _split_segments(head, ps):
        seg = seg.lstrip('({').rstrip(')}').strip() or seg
        toks = _strip_to_head(_tokens(seg, ps))
        verb = os.path.basename(toks[0]).lower() if toks else ''
        if verb.endswith('.exe'):
            verb = verb[:-4]
        low = [t.lower() for t in toks]
        for t in _outer_redirects(seg):
            if _opaque(t):
                out.append(('redirect', t))
        for t in _outer_redirects(seg, append=True):
            if _opaque(t):
                out.append(('append', t))
        if verb in ('bash', 'sh', 'zsh', 'dash'):
            k = next((k for k in range(1, len(toks) - 1) if toks[k] == '-c' or _SH_C_CLUSTER.fullmatch(toks[k])), None)
            if k is not None:
                out += unreadable_writes(toks[k + 1], cwd, 'bash')
                continue
        if verb in ('pwsh', 'powershell') and any(t in ('-command', '-c') for t in low[1:-1]):
            k = next(i for i, t in enumerate(low) if i and t in ('-command', '-c'))
            if k + 1 < len(toks):
                out += unreadable_writes(toks[k + 1], cwd, 'powershell')
            continue
        if verb == 'cmd':
            k = next((k for k in range(1, len(toks)) if low[k] in ('/c', '/k', '//c', '//k')), None)
            if k is not None and k + 1 < len(toks):
                out += unreadable_writes(' '.join(toks[k + 1:]), cwd, 'bash')
                continue
        if not toks or verb in ('cd', 'pushd', 'set-location', 'sl', 'chdir'):
            continue
        args = [t for t in toks[1:] if not t.startswith('-')]
        if verb == 'tee':
            out += [('tee', t) for t in args if _opaque(t)]
        elif verb == 'sed' and any(re.match(r'^-[a-zA-Z]*i', t) or t.startswith('--in-place') for t in toks[1:]):
            has_e = any(t in ('-e', '-f') or t.startswith('--expression') for t in toks[1:])
            out += [('sed -i', t) for t in (args if has_e else args[1:]) if _opaque(t)]
        elif verb == 'dd':
            out += [('dd', t[3:]) for t in toks[1:] if t.startswith('of=') and _opaque(t[3:])]
        elif verb in ('cp', 'mv', 'install') and shell != 'powershell' and len(args) >= 2:
            if _opaque(args[-1]):
                out.append((verb, args[-1]))
        else:
            psverb = PS_ALIASES.get(verb, verb) if (ps or verb in ('sc', 'cpi', 'mi', 'copy', 'move')) else verb
            if psverb in ('copy-item', 'move-item') or psverb in PS_WRITE_VERBS:
                named, rest = {}, []
                it = iter(toks[1:])
                for t in it:
                    tl = t.lower()
                    if tl.startswith('-') and tl not in PS_SWITCHES and ':' not in tl:
                        named[tl] = next(it, '')
                    elif not tl.startswith('-'):
                        rest.append(t)
                if psverb in ('copy-item', 'move-item'):
                    d = named.get('-destination') or (rest[1] if len(rest) > 1 else '')
                else:
                    d = named.get('-path') or named.get('-filepath') or named.get('-literalpath') or (rest[0] if rest else '')
                if _opaque(d):
                    out.append(('ps', d))
    return list(dict.fromkeys(out))
