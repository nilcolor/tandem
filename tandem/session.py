import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import subprocess
import unicodedata

from tandem import TandemError, paths

ROLES = ("driver", "navigator", "human")
PAIR_ROLES = ("driver", "navigator")
DEFAULT_ROLES = {"driver": "claude", "navigator": "agy"}
NAME_CAP = 48
DEFAULT_BRANCHES = ("main", "master")
SUBJECT_CAP = 100

# Order matters: it is the order the prefixes appear in profiles and prompts.
TEST_PREFIXES = (
    ("go.mod", "go test"),
    ("dip.yml", "dip rspec"),
)


def seconds(text, flag, positive=False):
    """A command-line duration in seconds; TandemError for anything but a non-negative number."""
    try:
        value = float(text)
    except ValueError:
        raise TandemError("%s must be a number of seconds, not %r" % (flag, text))
    if value != value or value < 0 or (positive and value == 0):
        raise TandemError("%s must %s" % (flag, "be positive" if positive else "not be negative"))
    return value


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso():
    return iso(_now())


def sanitize(text):
    # NFKD first so accented names keep their letters ("Café" → "cafe")
    # instead of losing them to the [a-z0-9] filter.
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:NAME_CAP].rstrip("-")


def git(cwd, *args):
    try:
        result = subprocess.run(
            ["git", "-C", cwd] + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except FileNotFoundError:
        raise TandemError("git not found on PATH")
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def repo_toplevel(cwd=None):
    top = git(cwd or os.getcwd(), "rev-parse", "--show-toplevel")
    if not top:
        raise TandemError("not inside a git repository")
    return os.path.realpath(top)


def current_branch(top):
    """The checked-out branch's short name, or None on detached HEAD."""
    return git(top, "symbolic-ref", "--quiet", "--short", "HEAD") or None


def _repo_slug(top):
    return sanitize(os.path.basename(top)) or "repo"


def derive_name(top, name=None, ticket=None):
    """The session name as the user would type it (unsanitized)."""
    if name is not None:
        return name
    if ticket is not None:
        return ticket
    branch = current_branch(top)
    if branch and branch not in DEFAULT_BRANCHES:
        tail = branch.rsplit("/", 1)[-1]
        if sanitize(tail):
            return tail
    return "%s-%s" % (_repo_slug(top), _now().astimezone().strftime("%m%d-%H%M"))


def detect_test_cmds(top):
    found = [cmd for marker, cmd in TEST_PREFIXES if os.path.exists(os.path.join(top, marker))]
    if os.path.exists(os.path.join(top, "Gemfile")) and not os.path.exists(os.path.join(top, "dip.yml")):
        found.append("bundle exec rspec")
    return found


def session_path(dir_name):
    return os.path.join(paths.sessions_dir(), dir_name)


def peer_dir(sdir, name):
    """`peers/<name>/` of a session: a peer's generated config, or a role's watcher state."""
    return os.path.join(sdir, "peers", name)


def load_session(dir_name):
    """session.json of `dir_name`, or None when the session doesn't exist.

    A name that isn't a plain entry of the spool (empty, `.`, `..`, anything with `/`) names no
    session, so no caller can read outside it.
    """
    if not dir_name or "/" in dir_name or dir_name in (".", ".."):
        return None
    path = os.path.join(session_path(dir_name), "session.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise TandemError("cannot read %s: %s" % (path, exc))


def save_session(meta):
    write_json_atomic(os.path.join(session_path(meta["dir"]), "session.json"), meta)


def is_active(meta):
    return meta is not None and meta.get("ended") is None


def session_state(dir_name):
    """(session.json or None, why it isn't active: "ended", "missing", "unreadable", or None).

    One classification for a repo pointer (`init`, resolution, `status`) and for waiting loops.
    """
    try:
        meta = load_session(dir_name)
    except TandemError:
        return None, "unreadable"
    if meta is None:
        return None, "missing"
    return meta, None if is_active(meta) else "ended"


def still_active(dir_name):
    """Whether a loop waiting on `dir_name` should keep waiting.

    An unreadable session.json ends the wait like an ended one: `deliver` refuses it too, so
    nothing more can arrive. It is replaced atomically, so this is never a half-written file.
    """
    return session_state(dir_name)[1] is None


def display_name(meta):
    """The session name as the user typed it, or the short name for a session without one."""
    return meta.get("name") or short_name(meta["dir"])


def read_text(path):
    """The file's text, or None when it is missing or can't be read as UTF-8."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _read_stripped(path):
    return (read_text(path) or "").strip() or None


def write_text_atomic(path, text):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_json_atomic(path, obj):
    write_text_atomic(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def repo_pointer_dir(top):
    key = hashlib.sha256(os.path.realpath(top).encode("utf-8")).hexdigest()[:16]
    return os.path.join(paths.repos_dir(), key)


def read_pointer(top):
    """The session dir name the repo's pointer names, or None."""
    return _read_stripped(os.path.join(repo_pointer_dir(top), "current"))


def write_pointer(top, dir_name):
    pdir = repo_pointer_dir(top)
    paths.make_private_dir(pdir)
    write_text_atomic(os.path.join(pdir, "path"), top + "\n")
    write_text_atomic(os.path.join(pdir, "current"), dir_name + "\n")


def clear_pointer(top, dir_name):
    """Remove the repo's pointer if it still names `dir_name`; a pointer that moved on is left alone."""
    if read_pointer(top) != dir_name:
        return
    pdir = repo_pointer_dir(top)
    for name in ("current", "path"):
        try:
            os.remove(os.path.join(pdir, name))
        except FileNotFoundError:
            pass
    try:
        os.rmdir(pdir)
    except OSError:
        pass


def repo_pointers():
    """(repo path or None, session dir name) for every repo pointer, by pointer key."""
    try:
        keys = sorted(os.listdir(paths.repos_dir()))
    except FileNotFoundError:
        return []
    found = []
    for key in keys:
        pdir = os.path.join(paths.repos_dir(), key)
        current = _read_stripped(os.path.join(pdir, "current"))
        if current:
            found.append((_read_stripped(os.path.join(pdir, "path")), current))
    return found


@contextlib.contextmanager
def locked(sdir):
    """Exclusive flock on `<session>/lock`: serializes id allocation and log appends."""
    with open(os.path.join(sdir, "lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def write_log_line(sdir, record):
    """Append one record to log.jsonl; the caller holds `locked(sdir)`."""
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(os.path.join(sdir, "log.jsonl"), "a", encoding="utf-8") as f:
        f.write(line)


def event_record(event, ts=None, **fields):
    record = {"kind": "event", "ts": ts or now_iso(), "event": event}
    record.update(fields)
    return record


def log_event(sdir, event, **fields):
    with locked(sdir):
        write_log_line(sdir, event_record(event, **fields))


def log_lines(sdir, offset=0):
    """Complete log.jsonl lines from byte `offset`, and the offset after the last one.

    A line without its newline yet is a write in progress; it is left for the
    next call instead of being read half-written.
    """
    try:
        with open(os.path.join(sdir, "log.jsonl"), "rb") as f:
            f.seek(offset)
            data = f.read()
    except FileNotFoundError:
        return [], offset
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    # Split on b"\n" only: str.splitlines would also break at U+2028 and
    # friends, which json.dumps(ensure_ascii=False) leaves unescaped.
    lines = [raw.decode("utf-8", "replace") for raw in data[:end].split(b"\n")]
    return lines, offset + end + 1


def read_log(sdir):
    """Parsed log.jsonl records, oldest first; lines that aren't JSON objects are skipped."""
    records = []
    for line in log_lines(sdir)[0]:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def watcher_pidfile(sdir, role):
    return os.path.join(peer_dir(sdir, role), "watch.pid")


def valid_pid(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def read_watcher(sdir, role):
    """The role's watcher pidfile, `{"pid": …, "ls_address": …}`, or None when missing or unreadable.

    `ls_address` is the agy language server a detached watcher serves; Claude's waiter has none.
    """
    try:
        with open(watcher_pidfile(sdir, role), encoding="utf-8") as f:
            value = json.loads(f.read())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or not valid_pid(value.get("pid")):
        return None
    return value


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _allocate_dir(base):
    """Create `sessions/<base>` (or `<base>-2`, `-3`, …) atomically; returns the dir name."""
    paths.make_private_dir(paths.sessions_dir())
    n = 1
    while True:
        dir_name = base if n == 1 else "%s-%d" % (base, n)
        try:
            os.mkdir(session_path(dir_name), 0o700)
            return dir_name
        except FileExistsError:
            n += 1


def _create_tree(sdir):
    for role in ROLES:
        for sub in ("tmp", "new", "cur"):
            os.makedirs(os.path.join(sdir, "inbox", role, sub))
    os.makedirs(os.path.join(sdir, "peers"))
    for name in ("log.jsonl", "lock"):
        open(os.path.join(sdir, name), "a").close()


def _normalise_test_cmds(cmds):
    """Whitespace collapsed, in order, once each: every reader then uses the list as stored."""
    out = [" ".join(c.split()) for c in cmds]
    if not all(out):
        raise TandemError("--test-cmd must not be empty")
    return list(dict.fromkeys(out))


def _end_replaced(old_dir, new_dir, now):
    """Mark the repo's previous session replaced; returns its dir, or None if it had already ended.

    Re-read under its own lock, like `end_session`, so a claim or `done` racing this isn't lost.
    """
    with locked(session_path(old_dir)):
        old, why = session_state(old_dir)
        if why is not None:
            return None
        old["ended"] = iso(now)
        old["replaced_by"] = new_dir
        save_session(old)
        write_log_line(session_path(old_dir), event_record("replaced", replaced_by=new_dir))
    return old_dir


def init_session(cwd=None, name=None, ticket=None, task=None, test_cmds=None, replace=False):
    """Create a session for the repo containing `cwd`; returns its session.json dict."""
    top = repo_toplevel(cwd)
    old_dir = read_pointer(top)
    old_active = session_state(old_dir)[1] is None
    if old_active and not replace:
        raise TandemError(
            "repo already has an active session %s (end it with `tandem done` or pass --replace)" % old_dir
        )

    display = derive_name(top, name, ticket)
    short = sanitize(display)
    if not short:
        raise TandemError("session name %r has no letters or digits" % display)

    now = _now()
    dir_name = _allocate_dir("%s--%s" % (_repo_slug(top), short))
    sdir = session_path(dir_name)
    _create_tree(sdir)
    meta = {
        "name": display,
        "dir": dir_name,
        "repo": top,
        "ticket": ticket,
        "task": task,
        "test_cmds": _normalise_test_cmds(test_cmds) if test_cmds is not None else detect_test_cmds(top),
        "roles": dict(DEFAULT_ROLES),
        "created": iso(now),
        "ended": None,
        "replaced_by": None,
    }
    save_session(meta)

    replaced = _end_replaced(old_dir, dir_name, now) if old_active else None

    write_pointer(top, dir_name)
    extra = {"replaces": replaced} if replaced else {}
    log_event(sdir, "init", session=dir_name, repo=top, **extra)
    return meta


def _read_task(args):
    if args.task_file is None:
        text = args.task
    else:
        try:
            with open(args.task_file, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise TandemError("cannot read --task-file %s: %s" % (args.task_file, exc))
    if text is None or not text.strip():
        return None
    return text


def configure_init(p):
    p.add_argument("name", nargs="?", help="session name (default: from the branch or --ticket)")
    p.add_argument("--ticket", metavar="KEY", help="ticket key, stored for intake")
    task = p.add_mutually_exclusive_group()
    task.add_argument("--task", metavar="TEXT", help="task brief")
    task.add_argument("--task-file", metavar="FILE", help="read the task brief from FILE")
    p.add_argument(
        "--test-cmd",
        metavar="CMD",
        action="append",
        dest="test_cmds",
        help="test command prefix (repeatable; replaces detection)",
    )
    p.add_argument("--replace", action="store_true", help="end the repo's active session and start a new one")


def run_init(args):
    meta = init_session(
        name=args.name,
        ticket=args.ticket,
        task=_read_task(args),
        test_cmds=args.test_cmds,
        replace=args.replace,
    )
    print(session_path(meta["dir"]))
    return 0


def short_name(dir_name):
    """The part of a session dir name the user types: `<repo>--<short>` → `<short>`."""
    # Sanitized slugs never contain "--", so the first one is the separator.
    return dir_name.split("--", 1)[1] if "--" in dir_name else dir_name


def all_sessions():
    """Every readable session.json under the spool, sorted by dir name."""
    try:
        names = sorted(os.listdir(paths.sessions_dir()))
    except FileNotFoundError:
        return []
    found = []
    for dir_name in names:
        try:
            meta = load_session(dir_name)
        except TandemError:
            continue
        if meta is not None:
            found.append(meta)
    return found


def active_sessions():
    return [m for m in all_sessions() if is_active(m)]


def _toplevel_or_none(cwd):
    try:
        return repo_toplevel(cwd)
    except TandemError:
        return None


def _describe(meta):
    return "%s (%s)" % (meta["dir"], meta.get("repo") or "?")


def _pick(matches, name):
    if len(matches) == 1:
        return matches[0]
    active = [m for m in matches if is_active(m)]
    if len(active) == 1:
        return active[0]
    listed = active or matches
    raise TandemError(
        "session name %r is ambiguous; use the full dir name: %s"
        % (name, ", ".join(_describe(m) for m in listed))
    )


def _by_name(name, top):
    meta = load_session(name)
    if meta is not None:
        return meta
    short = sanitize(name)
    if not short:
        raise TandemError("no session named %r" % name)
    sessions = [m for m in all_sessions() if short_name(m["dir"]) == short]
    if top is not None:
        local = [m for m in sessions if m.get("repo") == top]
        if local:
            return _pick(local, name)
    if not sessions:
        raise TandemError("no session named %r" % name)
    return _pick(sessions, name)


def resolve_session(explicit=None, cwd=None):
    """The session a command acts on, as its session.json dict.

    First match wins: `--session`, `TANDEM_SESSION`, the repo pointer (active
    sessions only), the only active session on the machine (outside a repo),
    else an error. Steps 1 and 2 return ended sessions too: the caller decides
    whether an ended session is an error, because hooks and watchers must exit
    quietly on one rather than fail.
    """
    top = _toplevel_or_none(cwd or os.getcwd())
    if explicit:
        return _by_name(explicit, top)

    env_dir = os.environ.get("TANDEM_SESSION")
    if env_dir:
        # Falling through to the pointer here would silently attach a launched
        # harness to some other session; a dangling TANDEM_SESSION is a bug.
        meta = load_session(env_dir)
        if meta is None:
            raise TandemError("TANDEM_SESSION names no session: %s" % env_dir)
        return meta

    stale = None
    if top is not None:
        pointed = read_pointer(top)
        if pointed:
            meta, why = session_state(pointed)
            if why is None:
                return meta
            stale = "%s (%s)" % (pointed, why)

    active = active_sessions()
    # Inside a repo the repo's own session is the only acceptable answer:
    # handing a launcher another repo's session (or the main checkout's, in a
    # worktree) would put two trees under one plan.
    if top is None and len(active) == 1:
        return active[0]

    lines = []
    if top is None:
        if active:
            lines.append("not inside a git repository and %d active sessions" % len(active))
        else:
            lines.append("not inside a git repository and no active session; run `tandem init` in a repo")
    else:
        lines.append("no active session for %s; run `tandem init`" % top)
    if stale:
        lines.append("stale repo pointer: %s" % stale)
    if active:
        lines.append("active sessions (pick one with --session):")
        lines.extend("  " + _describe(m) for m in active)
    raise TandemError("\n".join(lines))


def resolve_args(args):
    """`resolve_session` for a parsed command line (uses the global `--session`)."""
    return resolve_session(getattr(args, "session", None))


def _subject(body):
    """First non-blank line of a message body, capped for a one-line timeline entry."""
    for line in (body or "").splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= SUBJECT_CAP else line[: SUBJECT_CAP - 1].rstrip() + "…"
    return ""


def _quoted(text, indent="  "):
    return "\n".join((indent + "> " + line).rstrip() for line in (text or "").rstrip().splitlines())


def _read_decisions(sdir):
    try:
        with open(os.path.join(sdir, "decisions.md"), encoding="utf-8") as f:
            text = f.read().strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        return "_decisions.md could not be read: %s_" % exc
    return text or None


def build_summary(sdir, meta):
    """summary.md for an ending session: header facts, task, timeline, console input, decisions.

    Mechanical on purpose: the narrative is the driver's, written to
    decisions.md or a `done` message before it calls `tandem done`.
    """
    records = read_log(sdir)
    messages = [r for r in records if r.get("kind") == "message"]
    consoles = [r for r in records if r.get("kind") == "console"]
    wrap_up = [m for m in messages if m.get("type") == "done"]
    roles = meta["roles"]

    facts = [
        "- session: `%s`" % meta.get("dir"),
        "- repo: `%s`" % meta.get("repo"),
        "- ticket: %s" % (meta.get("ticket") or "none"),
        "- roles: %s" % (", ".join("%s %s" % (r, roles[r]) for r in ROLES if r in roles) or "none"),
        "- started: %s" % meta.get("created"),
        "- ended: %s" % meta.get("ended"),
    ]
    blocks = ["# %s" % meta.get("name"), "\n".join(facts)]

    blocks += ["## Task", (meta.get("task") or "").strip() or "_No task brief was recorded._"]

    timeline = []
    for m in messages:
        notes = [m.get("type") or "?"]
        if m.get("reply_to"):
            notes.append("reply to #%s" % m["reply_to"])
        line = "- `#%s` %s %s → %s, %s" % (m.get("id"), m.get("ts"), m.get("from"), m.get("to"), ", ".join(notes))
        subject = _subject(m.get("body"))
        timeline.append(line + (": " + subject if subject else ""))
    blocks += ["## Messages (%d)" % len(messages), "\n".join(timeline) or "_No messages._"]

    blocks.append("## Human input in consoles (%d)" % len(consoles))
    for c in consoles:
        blocks.append("- %s, to %s (%s console):" % (c.get("ts"), c.get("to"), c.get("peer")))
        blocks.append(_quoted(c.get("body")))
    if not consoles:
        blocks.append("_None._")

    if wrap_up:
        blocks.append("## Wrap-up")
        for m in wrap_up:
            blocks += ["From %s (`#%s`):" % (m.get("from"), m.get("id")), _quoted(m.get("body"), indent="")]

    blocks += ["## Decisions", _read_decisions(sdir) or "_decisions.md was not written._"]
    return "\n\n".join(b for b in blocks if b) + "\n"


def end_session(meta):
    """Write summary.md, mark the session ended, log `done`, clear the repo pointer.

    Everything but the pointer happens under the session lock, the same one
    `deliver` takes, so no message can land after the summary is written.
    """
    sdir = session_path(meta["dir"])
    with locked(sdir):
        current = load_session(meta["dir"])
        if current is None:
            raise TandemError("session %s no longer exists" % meta["dir"])
        if not is_active(current):
            raise TandemError("session %s already ended at %s" % (meta["dir"], current["ended"]))
        current["ended"] = now_iso()
        summary = os.path.join(sdir, "summary.md")
        write_text_atomic(summary, build_summary(sdir, current))
        save_session(current)
        write_log_line(sdir, event_record("done", ts=current["ended"], session=current["dir"]))
    if current.get("repo"):
        clear_pointer(current["repo"], current["dir"])
    return current


def run_done(args):
    meta = end_session(resolve_args(args))
    print(os.path.join(session_path(meta["dir"]), "summary.md"))
    return 0


def role_of(meta, peer):
    """The pair role `peer` holds in `meta`'s session, or None."""
    roles = meta["roles"]
    for role in PAIR_ROLES:
        if roles.get(role) == peer:
            return role
    return None


def live_claim(meta, peer):
    """`peer`'s claim record while the process that made it still runs, else None.

    A launcher claims with its own pid and then execs the harness, which keeps
    that pid, so a claim lasts exactly as long as the harness does.
    """
    claim = (meta.get("claims") or {}).get(peer)
    if not isinstance(claim, dict):
        return None
    pid = claim.get("pid")
    if not valid_pid(pid) or not pid_alive(pid):
        return None
    return claim


def claim_role(dir_name, peer, role=None, pid=None):
    """Make `peer` hold `role` (default: the role it holds now); returns the updated session.json.

    Taking the other peer's role swaps the two, unless that peer is running
    under a live claim of it. Shared by both launchers.
    """
    if role is not None and role not in PAIR_ROLES:
        raise TandemError("role must be one of %s, not %r" % (", ".join(PAIR_ROLES), role))
    pid = os.getpid() if pid is None else pid
    sdir = session_path(dir_name)
    with locked(sdir):
        meta = load_session(dir_name)
        if meta is None:
            raise TandemError("session %s no longer exists" % dir_name)
        if not is_active(meta):
            raise TandemError("session %s has ended; run `tandem init` to start a new one" % dir_name)
        roles = dict(meta["roles"])
        held = role_of(meta, peer)
        role = role or held
        if role is None:
            raise TandemError("%s holds no role in session %s; pass --role driver|navigator" % (peer, dir_name))
        holder = roles.get(role)
        if holder != peer:
            # Swapping a running peer's role would leave its harness on the wrong protocol.
            claim = live_claim(meta, holder) if holder else None
            if claim is not None:
                raise TandemError(
                    "%s is already the %s in session %s (pid %d); quit it first or pick the other role"
                    % (holder, role, dir_name, claim["pid"])
                )
            roles[role] = peer
            if held is not None:
                roles[held] = holder
        claims = dict(meta.get("claims") or {})
        claims[peer] = {"role": role, "pid": pid, "ts": now_iso()}
        meta["roles"] = roles
        meta["claims"] = claims
        save_session(meta)
        was = {"was": held} if held is not None and held != role else {}
        write_log_line(sdir, event_record("role_claimed", ts=claims[peer]["ts"], peer=peer, role=role, **was))
    return meta


def harness_extra(args, example):
    """A launcher's own-argument list after `--`; anything else left over is an error."""
    extra = list(args.extra or [])
    if extra and extra[0] == "--":
        return extra[1:]
    if extra:
        raise TandemError("pass the harness's own arguments after --, e.g. `%s`" % example)
    return extra


def harness_env(meta, role):
    """The caller's environment plus the session and role the harness and its hooks act as."""
    env = dict(os.environ)
    env["TANDEM_SESSION"] = meta["dir"]
    env["TANDEM_ROLE"] = role
    return env


def exec_harness(argv, env, cwd):
    """Replace this process with the harness; launchers take this as their injectable `exec`."""
    try:
        os.chdir(cwd)
    except OSError as exc:
        raise TandemError("cannot enter %s: %s" % (cwd, exc))
    try:
        os.execvpe(argv[0], argv, env)
    except OSError as exc:
        raise TandemError("cannot run %s: %s" % (argv[0], exc))
