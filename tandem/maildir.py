import json
import math
import os
import re
import sys
import time

from tandem import TandemError, session

TYPES = ("plan", "review_request", "review", "question", "answer", "decision", "escalate", "done")
EXPECTS_REPLY = frozenset(("plan", "review_request", "question", "escalate"))

_MESSAGE_FILE = re.compile(r"^(\d{4,})\.json$")

NO_MAIL = 3
POLL_INTERVAL = 0.5


def format_id(n):
    return "%04d" % n


def parse_id(text):
    """`7`, `0007` and `#0007` all name message 0007."""
    raw = str(text).strip().lstrip("#")
    if not raw.isdigit() or int(raw) == 0:
        raise TandemError("bad message id %r" % text)
    return format_id(int(raw))


def inbox_path(sdir, role, sub):
    return os.path.join(sdir, "inbox", role, sub)


def message_names(sdir, role, sub):
    """Message filenames in one inbox dir, lowest id first.

    Sorted by number, not text: ids widen past 9999 and "10000" < "9999".
    """
    try:
        names = os.listdir(inbox_path(sdir, role, sub))
    except FileNotFoundError:
        return []
    found = []
    for name in names:
        m = _MESSAGE_FILE.match(name)
        if m:
            found.append((int(m.group(1)), name))
    return [name for _, name in sorted(found)]


def _name_id(name):
    return int(_MESSAGE_FILE.match(name).group(1))


def unread_ids(sdir, role):
    """Ids of `role`'s unread messages, lowest first."""
    return [_name_id(name) for name in message_names(sdir, role, "new")]


def _message_ids(sdir):
    """Ids of every delivered message, in any inbox, read or not."""
    return {
        _name_id(name)
        for role in session.ROLES
        for sub in ("new", "cur")
        for name in message_names(sdir, role, sub)
    }


def check_role(role, flag):
    if role not in session.ROLES:
        raise TandemError("%s must be one of %s, not %r" % (flag, ", ".join(session.ROLES), role))
    return role


def default_type(reply_to):
    # A reply that defaulted to `question` would ask for a reply back, and two
    # agents answering each other's answers never stop.
    return "answer" if reply_to else "question"


def build_envelope(sender, to, body, type=None, reply_to=None, expects_reply=None, commit=None, files=None):
    """The envelope minus `id` and `ts`, which `deliver` stamps under the lock."""
    check_role(sender, "--as")
    check_role(to, "--to")
    if sender == to:
        raise TandemError("cannot send to yourself (%s)" % sender)
    reply_to = parse_id(reply_to) if reply_to is not None else None
    if type is None:
        type = default_type(reply_to)
    if type not in TYPES:
        raise TandemError("--type must be one of %s, not %r" % (", ".join(TYPES), type))
    if body is None or not body.strip():
        raise TandemError("message body is empty")
    refs = {}
    if commit:
        refs["commit"] = commit
    if files:
        refs["files"] = list(files)
    return {
        "from": sender,
        "to": to,
        "type": type,
        "reply_to": reply_to,
        "expects_reply": type in EXPECTS_REPLY if expects_reply is None else bool(expects_reply),
        "refs": refs,
        "body": body.lstrip("\r\n").rstrip(),
    }


def _write_synced(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def deliver(meta, envelope):
    """Stamp `id`/`ts`, deliver into the recipient's `new/`, log it; returns the envelope.

    One flock covers the ended check, id allocation, the rename and the log
    append, so ids are unique and gap-free and `log.jsonl` lists messages in
    id order.
    """
    sdir = session.session_path(meta["dir"])
    tmp_dir = inbox_path(sdir, envelope["to"], "tmp")
    new_dir = inbox_path(sdir, envelope["to"], "new")
    if not os.path.isdir(tmp_dir) or not os.path.isdir(new_dir):
        raise TandemError("session %s has no %s inbox" % (meta["dir"], envelope["to"]))
    with session.locked(sdir):
        # Re-read under the lock: `meta` may predate a `tandem done` that ran
        # while this command was parsing its body. Not `still_active`: an
        # unreadable session.json is refused with its own error, not as ended.
        if not session.is_active(session.load_session(meta["dir"])):
            raise TandemError("session %s has ended" % meta["dir"])
        ids = _message_ids(sdir)
        reply_to = envelope.get("reply_to")
        if reply_to is not None and int(reply_to) not in ids:
            raise TandemError("--reply-to %s: no such message in session %s" % (reply_to, meta["dir"]))
        msg = {"id": format_id(max(ids, default=0) + 1), "ts": session.now_iso()}
        msg.update(envelope)
        name = msg["id"] + ".json"
        tmp = os.path.join(tmp_dir, name)
        _write_synced(tmp, json.dumps(msg, ensure_ascii=False) + "\n")
        os.rename(tmp, os.path.join(new_dir, name))
        record = {"kind": "message"}
        record.update(msg)
        session.write_log_line(sdir, record)
    return msg


def _resolve_commit(meta, rev):
    top = meta.get("repo")
    if rev.startswith("-"):
        raise TandemError("--commit %s: not a commit" % rev)
    sha = session.git(top, "rev-parse", "--verify", "--quiet", "--short=12", rev + "^{commit}") if top else None
    if not sha:
        raise TandemError("--commit %s: not a commit in %s" % (rev, top or "the session repo"))
    return sha


def own_role(args, what):
    role = args.as_role or os.environ.get("TANDEM_ROLE")
    if not role:
        raise TandemError("no %s role: pass --as or run inside a tandem launcher (TANDEM_ROLE)" % what)
    return check_role(role, "--as")


def _read_body(args):
    if args.body is not None:
        return args.body
    # Blocking on a terminal would look like a hang; an agent always pipes or passes an arg.
    if sys.stdin is None or sys.stdin.isatty():
        raise TandemError("message body is empty (pass it as an argument or on stdin)")
    return sys.stdin.read()


def configure_send(p):
    p.add_argument("body", nargs="?", help="message body, markdown (default: stdin)")
    p.add_argument("--as", dest="as_role", metavar="ROLE", help="sender role (default: $TANDEM_ROLE)")
    p.add_argument("--to", required=True, metavar="ROLE", help="recipient role: %s" % ", ".join(session.ROLES))
    p.add_argument("--type", metavar="TYPE", help="%s (default: answer with --reply-to, else question)" % ", ".join(TYPES))
    p.add_argument("--reply-to", metavar="ID", help="id of the message this answers")
    p.add_argument("--commit", metavar="REV", help="commit the message is about (stored as a short sha)")
    p.add_argument("--file", dest="files", action="append", metavar="PATH", help="file the message refers to (repeatable)")
    reply = p.add_mutually_exclusive_group()
    reply.add_argument("--expects-reply", dest="expects_reply", action="store_const", const=True)
    reply.add_argument("--no-reply", dest="expects_reply", action="store_const", const=False)


def run_send(args):
    sender = own_role(args, "sender")
    meta = session.resolve_args(args)
    if not session.is_active(meta):
        raise TandemError("session %s has ended" % meta["dir"])
    envelope = build_envelope(
        sender,
        args.to,
        _read_body(args),
        type=args.type,
        reply_to=args.reply_to,
        expects_reply=args.expects_reply,
        commit=_resolve_commit(meta, args.commit) if args.commit else None,
        files=args.files,
    )
    msg = deliver(meta, envelope)
    print("sent %s to %s" % (msg["id"], msg["to"]))
    return 0


def read_message(path):
    """The envelope stored at `path`; FileNotFoundError passes through, anything else is a TandemError."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise TandemError("unreadable message %s: %s" % (path, exc))


def peek(sdir, role):
    """The lowest-id unread message for `role`, left in `new/`; None if there is none."""
    new_dir = inbox_path(sdir, role, "new")
    for name in message_names(sdir, role, "new"):
        try:
            return read_message(os.path.join(new_dir, name))
        except FileNotFoundError:
            # Consumed by a concurrent `recv` since the listing.
            continue
    return None


def pop(sdir, role):
    """Move the lowest-id unread message for `role` to `cur/` and return it; None if there is none.

    The rename is the claim: of two readers racing for one file exactly one
    rename succeeds, and the loser tries the next file.
    """
    new_dir = inbox_path(sdir, role, "new")
    cur_dir = inbox_path(sdir, role, "cur")
    for name in message_names(sdir, role, "new"):
        dest = os.path.join(cur_dir, name)
        try:
            os.rename(os.path.join(new_dir, name), dest)
        except FileNotFoundError:
            continue
        try:
            return read_message(dest)
        except (OSError, TandemError) as exc:
            # Left in cur/ so its id stays taken; the next read moves on to the next message.
            raise TandemError("%s (set aside as read; read again for the next message)" % exc)
    return None


def _head_and_refs(msg):
    head = "#%s %s → %s  %s" % (msg.get("id"), msg.get("from"), msg.get("to"), msg.get("type"))
    notes = []
    if msg.get("reply_to"):
        notes.append("reply to #%s" % msg["reply_to"])
    if msg.get("expects_reply"):
        notes.append("expects reply")
    if notes:
        head += "  " + ", ".join(notes)
    lines = []
    refs = msg.get("refs") or {}
    if refs.get("commit"):
        lines.append("commit: %s" % refs["commit"])
    if refs.get("files"):
        lines.append("files: %s" % ", ".join(refs["files"]))
    return head, lines


def render(msg):
    """Header line, refs, blank line, body."""
    head, refs = _head_and_refs(msg)
    return "\n".join([head] + refs + ["", msg.get("body") or ""])


def receive(meta, role, consume=True, wait=False, timeout=None):
    """Next message for `role`, or None when there is none (after waiting, if asked).

    A wait gives up once the session has ended, since `deliver` refuses ended
    sessions and nothing more can arrive.
    """
    sdir = session.session_path(meta["dir"])
    if not os.path.isdir(inbox_path(sdir, role, "new")):
        raise TandemError("session %s has no %s inbox" % (meta["dir"], role))
    take = pop if consume else peek
    deadline = time.monotonic() + (math.inf if timeout is None else timeout)
    while True:
        msg = take(sdir, role)
        if msg is not None or not wait:
            return msg
        if not session.still_active(meta["dir"]):
            return None
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        time.sleep(min(POLL_INTERVAL, left))


def configure_recv(p):
    p.add_argument("--as", dest="as_role", metavar="ROLE", help="whose inbox to read (default: $TANDEM_ROLE)")
    p.add_argument("--peek", action="store_true", help="print the next message without consuming it")
    p.add_argument("--wait", action="store_true", help="poll until a message arrives")
    p.add_argument("--timeout", metavar="S", help="with --wait, give up after S seconds (implies --wait)")
    p.add_argument("--json", action="store_true", help="print the raw envelope")


def run_recv(args):
    role = own_role(args, "reader")
    timeout = session.seconds(args.timeout, "--timeout") if args.timeout is not None else None
    meta = session.resolve_args(args)
    msg = receive(meta, role, consume=not args.peek, wait=args.wait or timeout is not None, timeout=timeout)
    if msg is None:
        # stderr, so stdout carries only messages and `--json` output stays parseable.
        print("no mail for %s" % role, file=sys.stderr)
        return NO_MAIL
    print(json.dumps(msg, ensure_ascii=False) if args.json else render(msg))
    return 0


def _indented(first, rest):
    return "\n".join([first] + [("    " + line).rstrip() for line in rest])


def _field(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def render_record(line):
    """One log.jsonl line as text: a timestamped head line, details indented below it.

    A line that isn't a known record is shown raw rather than dropped, so a
    damaged log still reads end to end.
    """
    try:
        record = json.loads(line)
    except ValueError:
        record = None
    if not isinstance(record, dict) or record.get("kind") not in ("message", "console", "event"):
        return "? " + line
    ts = record.get("ts") or "?"
    kind = record["kind"]
    if kind == "message":
        head, refs = _head_and_refs(record)
        return _indented("%s  %s" % (ts, head), refs + (record.get("body") or "").splitlines())
    if kind == "console":
        head = "%s  %s → %s  console (%s)" % (ts, record.get("from"), record.get("to"), record.get("peer"))
        return _indented(head, (record.get("body") or "").splitlines())
    extra = " ".join(
        "%s=%s" % (k, _field(v)) for k, v in record.items() if k not in ("kind", "ts", "event")
    )
    return ("%s  * %s %s" % (ts, record.get("event"), extra)).rstrip()


def follow_log(meta, emit, follow=False, interval=POLL_INTERVAL):
    """Feed every complete log line to `emit`; with `follow`, keep reading by byte offset.

    Following stops once the session has ended and the log is drained:
    nothing more can be sent, and an endless `-f` on a finished session would
    only hold the pane.
    """
    sdir = session.session_path(meta["dir"])
    offset = 0
    while True:
        lines, offset = session.log_lines(sdir, offset)
        for line in lines:
            emit(line)
        if not follow:
            return
        if lines:
            continue
        if not session.still_active(meta["dir"]):
            # `done` writes its event under the lock after marking the session
            # ended; taking the lock waits for that write to finish.
            with session.locked(sdir):
                pass
            for line in session.log_lines(sdir, offset)[0]:
                emit(line)
            return
        time.sleep(interval)


def configure_log(p):
    p.add_argument("--json", action="store_true", help="print log.jsonl lines as stored")
    p.add_argument("-f", "--follow", action="store_true", help="keep printing new lines until the session ends")


def run_log(args):
    meta = session.resolve_args(args)

    def emit(line):
        print(line if args.json else render_record(line))
        if args.follow:
            sys.stdout.flush()

    try:
        follow_log(meta, emit, follow=args.follow)
    except KeyboardInterrupt:
        pass
    return 0


def pending_counts(sdir):
    return {role: len(message_names(sdir, role, "new")) for role in session.ROLES}


def last_messages(sdir):
    """The latest logged message sent by each role."""
    last = {}
    for record in session.read_log(sdir):
        if record.get("kind") == "message" and record.get("from") in session.ROLES:
            last[record["from"]] = record
    return last


def watcher_state(sdir, role):
    if not os.path.lexists(session.watcher_pidfile(sdir, role)):
        return "not running"
    info = session.read_watcher(sdir, role)
    if info is None:
        return "unreadable pidfile"
    pid = info["pid"]
    if session.pid_alive(pid):
        return "running (pid %d)" % pid
    return "dead (stale pid %d)" % pid


def describe_session(meta):
    sdir = session.session_path(meta["dir"])
    roles = meta["roles"]
    head = "%s  %s" % (meta["dir"], meta.get("repo") or "?")
    if not session.is_active(meta):
        head += "  (ended %s)" % meta["ended"]
    facts = ["name %s" % meta.get("name")]
    if meta.get("ticket"):
        facts.append("ticket %s" % meta["ticket"])
    facts.append("created %s" % meta.get("created"))
    lines = [head, "  " + ", ".join(facts)]
    lines.append("  roles: " + ", ".join("%s %s" % (r, roles[r]) for r in session.ROLES if r in roles))
    counts = pending_counts(sdir)
    lines.append("  pending: " + ", ".join("%s %d" % (r, counts[r]) for r in session.ROLES))
    last = last_messages(sdir)
    for role in session.ROLES:
        msg = last.get(role)
        if msg is None:
            lines.append("  last from %s: none" % role)
        else:
            lines.append(
                "  last from %s: #%s %s → %s  %s" % (role, msg.get("id"), msg.get("type"), msg.get("to"), msg.get("ts"))
            )
    lines.append(
        "  watchers: " + ", ".join("%s %s" % (r, watcher_state(sdir, r)) for r in session.PAIR_ROLES)
    )
    return "\n".join(lines)


def stale_pointers():
    """(repo path, session dir, reason) for pointers naming an ended, missing or unreadable session."""
    stale = []
    for repo, dir_name in session.repo_pointers():
        why = session.session_state(dir_name)[1]
        if why is not None:
            stale.append((repo, dir_name, why))
    return stale


def run_status(args):
    # Global by default: status is how the user finds sessions, so it must not
    # depend on which repo the pane is in. --session narrows it to one.
    if getattr(args, "session", None):
        sessions = [session.resolve_args(args)]
        stale = []
    else:
        sessions = session.active_sessions()
        stale = stale_pointers()
    blocks = [describe_session(m) for m in sessions] or ["no active sessions"]
    if stale:
        lines = ["stale repo pointers (`tandem init` in the repo starts a new session):"]
        lines += ["  %s → %s (%s)" % (repo or "?", dir_name, reason) for repo, dir_name, reason in stale]
        blocks.append("\n".join(lines))
    print("\n\n".join(blocks))
    return 0
