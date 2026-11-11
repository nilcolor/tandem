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
DEFAULT_ROLES = {"driver": "claude", "navigator": "agy"}
NAME_CAP = 48
DEFAULT_BRANCHES = ("main", "master")

# Order matters: it is the order the prefixes appear in profiles and prompts.
TEST_PREFIXES = (
    ("go.mod", "go test"),
    ("dip.yml", "dip rspec"),
)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize(text, cap=NAME_CAP):
    # NFKD first so accented names keep their letters ("Café" → "cafe")
    # instead of losing them to the [a-z0-9] filter.
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:cap].rstrip("-")


def _git(cwd, *args):
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
    top = _git(cwd or os.getcwd(), "rev-parse", "--show-toplevel")
    if not top:
        raise TandemError("not inside a git repository")
    return os.path.realpath(top)


def current_branch(top):
    """The checked-out branch's short name, or None on detached HEAD."""
    return _git(top, "symbolic-ref", "--quiet", "--short", "HEAD") or None


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


def load_session(dir_name):
    """session.json of `dir_name`, or None when the session doesn't exist."""
    try:
        with open(os.path.join(session_path(dir_name), "session.json"), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_session(meta):
    write_json_atomic(os.path.join(session_path(meta["dir"]), "session.json"), meta)


def is_active(meta):
    return meta is not None and meta.get("ended") is None


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
    try:
        with open(os.path.join(repo_pointer_dir(top), "current"), encoding="utf-8") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def write_pointer(top, dir_name):
    pdir = repo_pointer_dir(top)
    os.makedirs(pdir, exist_ok=True)
    write_text_atomic(os.path.join(pdir, "path"), top + "\n")
    write_text_atomic(os.path.join(pdir, "current"), dir_name + "\n")


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


def log_event(sdir, event, **fields):
    record = {"kind": "event", "ts": iso(_now()), "event": event}
    record.update(fields)
    with locked(sdir):
        write_log_line(sdir, record)


def _allocate_dir(base):
    """Create `sessions/<base>` (or `<base>-2`, `-3`, …) atomically; returns the dir name."""
    os.makedirs(paths.sessions_dir(), exist_ok=True)
    n = 1
    while True:
        dir_name = base if n == 1 else "%s-%d" % (base, n)
        try:
            os.mkdir(session_path(dir_name))
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


def init_session(cwd=None, name=None, ticket=None, task=None, test_cmds=None, replace=False):
    """Create a session for the repo containing `cwd`; returns its session.json dict."""
    top = repo_toplevel(cwd)
    old_dir = read_pointer(top)
    old = load_session(old_dir) if old_dir else None
    if is_active(old) and not replace:
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
        "test_cmds": list(test_cmds) if test_cmds is not None else detect_test_cmds(top),
        "roles": dict(DEFAULT_ROLES),
        "created": iso(now),
        "ended": None,
        "replaced_by": None,
    }
    save_session(meta)

    replaced = None
    if is_active(old):
        old["ended"] = iso(now)
        old["replaced_by"] = dir_name
        save_session(old)
        log_event(session_path(old_dir), "replaced", replaced_by=dir_name)
        replaced = old_dir

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
    test_cmds = None
    if args.test_cmds is not None:
        test_cmds = [c.strip() for c in args.test_cmds]
        if not all(test_cmds):
            raise TandemError("--test-cmd must not be empty")
    meta = init_session(
        name=args.name,
        ticket=args.ticket,
        task=_read_task(args),
        test_cmds=test_cmds,
        replace=args.replace,
    )
    print(session_path(meta["dir"]))
    return 0
