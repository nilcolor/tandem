"""Shell command-line analysis for agy's PreToolUse hook: the bypass test and the write gate.

Both read the command the way `sh` would (quotes, escapes, separators), so a
quoted message body stays one literal argument. The bypass test fails closed
on anything it can't read; the write gate is best effort.
"""

import os
import re
import shlex

WORD, SEP, REDIR = "word", "sep", "redir"

_SPACE = " \t"
_DQ_ESCAPES = '$`"\\\n'
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Shell keywords and wrappers that run the word after them as the command.
_KEYWORDS = frozenset(("!", "{", "}", "if", "then", "else", "elif", "do", "while", "until", "time"))
_WRAPPERS = frozenset(("command", "builtin", "exec", "nohup", "nice", "env", "sudo", "doas", "xargs", "timeout"))

WRITERS = frozenset(
    (
        "touch", "rm", "mv", "cp", "mkdir", "rmdir", "ln", "chmod", "chown", "chgrp", "truncate",
        "dd", "tee", "install", "patch", "unlink", "shred", "rsync",
    )
)
# Subcommands that change the work tree, the index, refs or a remote. Read-only
# forms of stash, tag and branch are told apart in _git_writes.
GIT_WRITERS = frozenset(
    (
        "commit", "checkout", "switch", "reset", "restore", "stash", "apply", "am", "merge",
        "rebase", "cherry-pick", "revert", "clean", "add", "rm", "mv", "tag", "branch", "push",
        "pull", "fetch", "init", "clone", "worktree", "submodule", "update-ref", "update-index",
        "gc", "prune",
    )
)
_GIT_VALUE_OPTS = frozenset(("-C", "-c", "--git-dir", "--work-tree", "--namespace"))
_GIT_BRANCH_WRITES = frozenset(
    (
        "-d", "-D", "-m", "-M", "-c", "-C", "-f", "-u", "--delete", "--move", "--copy", "--force",
        "--set-upstream-to", "--unset-upstream", "--edit-description",
    )
)
_GIT_BRANCH_LISTS = frozenset(("-l", "--list", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at"))
_HARMLESS_TARGETS = frozenset(("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"))
_WRITE_REDIRS = frozenset((">", ">>", ">|", "&>", "&>>", "<>"))


class Unreadable(ValueError):
    """The command can't be tokenized (an unterminated quote)."""


def tokens(text):
    """`(kind, value)` tokens of `text` and whether it expands anything.

    Kinds: WORD (the unquoted value), SEP (`;` `&&` `||` `|` `|&` `&`
    newline `(` `)` `$(` and backtick), REDIR (the operator, fd included;
    the next WORD is its target). Expansion is any `$` or backtick outside
    single quotes. Command substitutions and subshells become SEP-delimited
    stretches, so the commands inside them are seen as commands.
    """
    out = []
    word = []
    started = False
    expands = False
    stack = []
    i = 0
    n = len(text)

    def flush():
        nonlocal started
        if started:
            out.append((WORD, "".join(word)))
        del word[:]
        started = False

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if stack and stack[-1] == "dq":
            if c == '"':
                stack.pop()
                i += 1
            elif c == "\\" and nxt and nxt in _DQ_ESCAPES:
                if nxt != "\n":
                    word.append(nxt)
                i += 2
            elif c == "$" and nxt == "(":
                expands = True
                flush()
                out.append((SEP, "$("))
                stack.append("paren")
                i += 2
            elif c == "`":
                expands = True
                flush()
                out.append((SEP, "`"))
                stack.append("bq")
                i += 1
            else:
                if c == "$":
                    expands = True
                word.append(c)
                i += 1
            continue

        if c in _SPACE:
            flush()
            i += 1
        elif c == "\n":
            flush()
            out.append((SEP, "\n"))
            i += 1
        elif c == "'":
            end = text.find("'", i + 1)
            if end < 0:
                raise Unreadable("unterminated single quote")
            word.append(text[i + 1 : end])
            started = True
            i = end + 1
        elif c == '"':
            stack.append("dq")
            started = True
            i += 1
        elif c == "\\":
            if nxt == "\n":
                i += 2
            elif nxt:
                word.append(nxt)
                started = True
                i += 2
            else:
                word.append(c)
                started = True
                i += 1
        elif c == "#" and not started:
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif c == ";":
            flush()
            out.append((SEP, ";"))
            i += 1
        elif c == "&":
            flush()
            if nxt == "&":
                out.append((SEP, "&&"))
                i += 2
            elif nxt == ">":
                op = "&>>" if text.startswith(">>", i + 1) else "&>"
                out.append((REDIR, op))
                i += len(op)
            else:
                out.append((SEP, "&"))
                i += 1
        elif c == "|":
            flush()
            op = c + nxt if nxt in "|&" and nxt else c
            out.append((SEP, op))
            i += len(op)
        elif c in "<>":
            fd = ""
            if started and word and "".join(word).isdigit():
                fd = "".join(word)
                del word[:]
                started = False
            flush()
            if nxt == "(":
                expands = True
                out.append((SEP, c + "("))
                stack.append("paren")
                i += 2
                continue
            if c == ">":
                op = ">" + nxt if nxt and nxt in ">|&" else ">"
            elif text.startswith("<<<", i):
                op = "<<<"
            else:
                op = "<" + nxt if nxt and nxt in "<&>" else "<"
            out.append((REDIR, fd + op))
            i += len(op)
        elif c == "(":
            flush()
            out.append((SEP, "("))
            stack.append("paren")
            i += 1
        elif c == ")":
            flush()
            out.append((SEP, ")"))
            if stack and stack[-1] == "paren":
                stack.pop()
            i += 1
        elif c == "`":
            expands = True
            flush()
            out.append((SEP, "`"))
            if stack and stack[-1] == "bq":
                stack.pop()
            else:
                stack.append("bq")
            i += 1
        elif c == "$" and nxt == "(":
            expands = True
            flush()
            out.append((SEP, "$("))
            stack.append("paren")
            i += 2
        else:
            if c == "$":
                expands = True
            word.append(c)
            started = True
            i += 1
    if "dq" in stack:
        raise Unreadable("unterminated double quote")
    flush()
    return out, expands


def prefix_words(prefix):
    """A configured prefix (`go test`, `tandem`) as words; None if it doesn't parse."""
    try:
        words = shlex.split(prefix)
    except ValueError:
        return None
    return words or None


def bypass_prefix(text, prefixes):
    """The prefix `text` runs under, if it is one plain command starting with it; else None.

    Plain means no separator, redirect or expansion outside quotes: one
    command whose words are all literal, so nothing but that command can run
    outside the sandbox. Matched on whole words, so `tandemx` isn't `tandem`.
    """
    try:
        toks, expands = tokens(text.strip())
    except Unreadable:
        return None
    if expands or any(kind != WORD for kind, _ in toks):
        return None
    words = [value for _, value in toks]
    for prefix in prefixes:
        want = prefix_words(prefix)
        if want and words[: len(want)] == want:
            return prefix
    return None


def segments(toks):
    """[(words, redirects)] per simple command; a redirect is (op, target or None)."""
    found = []
    words, redirs = [], []
    pending = None
    for kind, value in toks:
        if kind == SEP:
            if pending is not None:
                redirs.append((pending, None))
                pending = None
            if words or redirs:
                found.append((words, redirs))
            words, redirs = [], []
        elif kind == REDIR:
            if pending is not None:
                redirs.append((pending, None))
            pending = value
        elif pending is not None:
            redirs.append((pending, value))
            pending = None
        else:
            words.append(value)
    if pending is not None:
        redirs.append((pending, None))
    if words or redirs:
        found.append((words, redirs))
    return found


def _command(words):
    """(command name, its arguments), skipping assignments, keywords and wrappers."""
    i = 0
    while i < len(words):
        w = words[i]
        if _ASSIGNMENT.match(w) or w in _KEYWORDS:
            i += 1
            continue
        name = os.path.basename(w)
        if name in _WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or _ASSIGNMENT.match(words[i])):
                i += 1
            if name == "timeout" and i < len(words):
                i += 1
            continue
        return name, words[i + 1 :]
    return None, []


def _short_flag_has(arg, letter, value_letters):
    """Whether a short-option cluster like `-ni` or `-pi.bak` sets `letter`.

    Scanning stops at a letter that takes a value, so `-Ilib` isn't `-i`.
    """
    if not arg.startswith("-") or arg.startswith("--") or len(arg) < 2:
        return False
    for ch in arg[1:]:
        if ch == letter:
            return True
        if ch in value_letters or not ch.isalnum():
            return False
    return False


def _in_place(name, args):
    if name == "sed":
        value_letters, script_letters, long_flag = "efl", "ef", "--in-place"
    elif name == "perl":
        value_letters, script_letters, long_flag = "eEIlmMxFCdD0", "eE", None
    else:
        return False
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if long_flag and (arg == long_flag or arg.startswith(long_flag + "=")):
            return True
        if _short_flag_has(arg, "i", value_letters):
            return True
        # The word after `-e`/`-f` is a script or file name, not options.
        if len(arg) > 1 and arg.startswith("-") and not arg.startswith("--") and arg[-1] in script_letters:
            skip = True
    return False


def _git_writes(args):
    i = 0
    while i < len(args):
        a = args[i]
        if a in _GIT_VALUE_OPTS:
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            break
    if i >= len(args):
        return None
    sub, rest = args[i], args[i + 1 :]
    if sub not in GIT_WRITERS:
        return None
    if sub == "stash" and rest[:1] and rest[0] in ("list", "show"):
        return None
    if sub == "tag" and (not rest or rest[0] in ("-l", "--list", "-n")):
        return None
    if sub == "branch":
        if any(a in _GIT_BRANCH_WRITES or a.startswith("--set-upstream-to=") for a in rest):
            return "git branch"
        positional = [a for a in rest if not a.startswith("-")]
        if not positional or any(a in _GIT_BRANCH_LISTS for a in rest):
            return None
    return "git " + sub


def _find_writes(args):
    for i, a in enumerate(args):
        if a == "-delete":
            return True
        if a in ("-exec", "-execdir", "-ok", "-okdir") and i + 1 < len(args):
            if os.path.basename(args[i + 1]) in WRITERS:
                return True
    return False


def _redirect_writes(op, target):
    bare = op.lstrip("0123456789")
    if bare == ">&":
        return target is not None and not target.isdigit() and target != "-" and target not in _HARMLESS_TARGETS
    if bare not in _WRITE_REDIRS:
        return False
    return target not in _HARMLESS_TARGETS


def write_reason(text):
    """Why `text` looks like it writes files, or None. Best effort, never raises.

    Looks at every simple command (including those in `$(...)`, backticks and
    subshells): a known writer, `sed -i`/`perl -i`, a tree- or ref-changing
    git subcommand, `find -delete`/`-exec <writer>`, or an output redirect to
    anything but /dev/null and friends.
    """
    try:
        toks, _ = tokens(text)
    except Unreadable:
        return None
    for words, redirs in segments(toks):
        for op, target in redirs:
            if _redirect_writes(op, target):
                return "redirect %s%s" % (op, " " + target if target else "")
        name, args = _command(words)
        if name is None:
            continue
        if name in WRITERS:
            return name
        if _in_place(name, args):
            return "%s -i" % name
        if name == "find" and _find_writes(args):
            return "find"
        if name == "git":
            reason = _git_writes(args)
            if reason:
                return reason
    return None
