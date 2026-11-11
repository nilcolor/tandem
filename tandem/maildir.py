import json
import os
import re
import sys

from tandem import TandemError, session

TYPES = ("plan", "review_request", "review", "question", "answer", "decision", "escalate", "done")
EXPECTS_REPLY = frozenset(("plan", "review_request", "question", "escalate"))

_MESSAGE_FILE = re.compile(r"^(\d{4,})\.json$")


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


def _message_ids(sdir):
    """Ids of every delivered message, in any inbox, read or not."""
    ids = set()
    for role in session.ROLES:
        for sub in ("new", "cur"):
            try:
                names = os.listdir(inbox_path(sdir, role, sub))
            except FileNotFoundError:
                continue
            for name in names:
                m = _MESSAGE_FILE.match(name)
                if m:
                    ids.add(int(m.group(1)))
    return ids


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
        # while this command was parsing its body.
        if not session.is_active(session.load_session(meta["dir"])):
            raise TandemError("session %s has ended" % meta["dir"])
        ids = _message_ids(sdir)
        reply_to = envelope.get("reply_to")
        if reply_to is not None and int(reply_to) not in ids:
            raise TandemError("--reply-to %s: no such message in session %s" % (reply_to, meta["dir"]))
        msg = {"id": format_id(max(ids, default=0) + 1), "ts": session.iso(session._now())}
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
    sha = session._git(top, "rev-parse", "--verify", "--quiet", "--short=12", rev + "^{commit}") if top else None
    if not sha:
        raise TandemError("--commit %s: not a commit in %s" % (rev, top or "the session repo"))
    return sha


def sender_role(args):
    role = args.as_role or os.environ.get("TANDEM_ROLE")
    if not role:
        raise TandemError("no sender role: pass --as or run inside a tandem launcher (TANDEM_ROLE)")
    return role


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
    sender = check_role(sender_role(args), "--as")
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
