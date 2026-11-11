import os
import re
import select
import sys
from collections import namedtuple

from tandem import TandemError, maildir, session

TICK = 1.0
SENDER = "human"

HELP = """\
  @driver <text>        send to the driver (bare text does the same)
  @navigator <text>     send to the navigator
  @role:<type> <text>   send with an explicit type (%s)
  /log                  print the session log
  /status               print this session's status
  /help                 show this help
  /quit                 leave (Ctrl-D and Ctrl-C too)
Without a type, a message answers the latest message you read from that role, or is a decision
when there is none.""" % ", ".join(maildir.TYPES)

Command = namedtuple("Command", "kind to type body")

_ADDRESS = re.compile(r"@([^\s:]*)(?::(\S*))?(?:\s+(.*))?\Z", re.DOTALL)
_SLASH = {"/quit": "quit", "/log": "log", "/status": "status", "/help": "help"}


def parse_line(line):
    """One input line as a Command; TandemError for input that can't be sent."""
    text = line.strip()
    if not text:
        return Command("empty", None, None, None)
    if text.startswith("/"):
        word, _, rest = text.partition(" ")
        kind = _SLASH.get(word.lower())
        if kind is None:
            raise TandemError("unknown command %s (try /help)" % word)
        if rest.strip():
            raise TandemError("%s takes no arguments" % word)
        return Command(kind, None, None, None)
    if not text.startswith("@"):
        return Command("send", "driver", None, text)
    m = _ADDRESS.match(text)
    role = m.group(1).lower()
    if role == SENDER:
        raise TandemError("cannot send to yourself (human)")
    if role not in session.PAIR_ROLES:
        # A typo'd role must not fall through to the driver as bare text.
        raise TandemError("unknown role @%s: use @driver or @navigator" % m.group(1))
    type = m.group(2)
    if type is not None:
        type = type.lower()
        if type not in maildir.TYPES:
            raise TandemError("unknown type %r: use one of %s" % (m.group(2), ", ".join(maildir.TYPES)))
    body = (m.group(3) or "").strip()
    if not body:
        raise TandemError("nothing to send to %s" % role)
    return Command("send", role, type, body)


def latest_from(sdir, role):
    """Id of the newest message from `role` the human has read (its `cur/`), or None.

    Unread mail is left out: an answer must point at something the user saw.
    """
    cur = maildir.inbox_path(sdir, SENDER, "cur")
    for name in reversed(maildir.message_names(sdir, SENDER, "cur")):
        try:
            msg = maildir.read_message(os.path.join(cur, name))
        except TandemError:
            continue
        if isinstance(msg, dict) and msg.get("from") == role:
            return msg.get("id")
    return None


def choose_reply(sdir, cmd):
    """(type, reply_to) for a send: the grammar's defaults, applied to the human inbox."""
    if cmd.type is None:
        reply_to = latest_from(sdir, cmd.to)
        return ("answer", reply_to) if reply_to else ("decision", None)
    if cmd.type == "answer":
        return "answer", latest_from(sdir, cmd.to)
    return cmd.type, None


class FdLines:
    """Line source over a file descriptor: waits up to `timeout` for input.

    Reads the fd directly instead of `sys.stdin.readline()`: lines that sit in
    Python's buffer are invisible to select, so a pasted batch would stall
    until the next keystroke.
    """

    def __init__(self, fd):
        self._fd = fd
        self._buf = b""
        self._eof = False

    def poll(self, timeout):
        """New complete lines ([] when none arrived in time), or None at end of input."""
        if self._eof:
            return None
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return []
        data = os.read(self._fd, 65536)
        if not data:
            self._eof = True
            rest, self._buf = self._buf, b""
            return [_decode(rest)] if rest else None
        self._buf += data
        *lines, self._buf = self._buf.split(b"\n")
        return [_decode(raw) for raw in lines]


def _decode(raw):
    return raw.decode("utf-8", "replace").rstrip("\r")


class Repl:
    def __init__(self, meta, source, out=None, err=None):
        self.dir = meta["dir"]
        self.sdir = session.session_path(self.dir)
        self.source = source
        self.out = out or sys.stdout
        self.err = err or sys.stderr

    def say(self, text, stream=None):
        stream = stream or self.out
        stream.write(text + "\n")
        stream.flush()

    def complain(self, exc):
        self.say("tandem: %s" % exc, self.err)

    def load(self):
        meta = session.load_session(self.dir)
        if meta is None:
            raise TandemError("session %s no longer exists" % self.dir)
        return meta

    def banner(self, meta):
        peers = ", ".join("%s %s" % (r, meta["roles"][r]) for r in session.PAIR_ROLES)
        return "tandem human: session %s (%s)\n%s" % (self.dir, peers, HELP)

    def show_mail(self):
        while True:
            try:
                msg = maildir.pop(self.sdir, SENDER)
            except TandemError as exc:
                # pop has set the bad file aside, so the next pop reads the next message.
                self.complain(exc)
                continue
            if msg is None:
                return
            text = maildir.render(msg)
            if msg.get("expects_reply") and msg.get("from") in session.PAIR_ROLES:
                text += "\n(reply: @%s <text>)" % msg["from"]
            self.say("\n" + text + "\n")

    def tick(self, timeout=TICK):
        """Show new mail, then wait up to `timeout` for input and act on it; False means stop."""
        meta = self.load()
        self.show_mail()
        if not session.is_active(meta):
            how = "replaced by %s" % meta["replaced_by"] if meta.get("replaced_by") else "ended"
            self.say("session %s %s at %s" % (self.dir, how, meta["ended"]))
            return False
        lines = self.source.poll(timeout)
        if lines is None:
            return False
        for line in lines:
            if not self.handle(line):
                return False
        return True

    def handle(self, line):
        try:
            cmd = parse_line(line)
        except TandemError as exc:
            self.complain(exc)
            return True
        if cmd.kind == "quit":
            return False
        try:
            if cmd.kind == "send":
                self.send(cmd)
            elif cmd.kind == "log":
                meta = self.load()
                maildir.follow_log(meta, lambda line: self.say(maildir.render_record(line)))
            elif cmd.kind == "status":
                self.say(maildir.describe_session(self.load()))
            elif cmd.kind == "help":
                self.say(HELP)
        except TandemError as exc:
            self.complain(exc)
        return True

    def send(self, cmd):
        meta = self.load()
        type, reply_to = choose_reply(self.sdir, cmd)
        envelope = maildir.build_envelope(SENDER, cmd.to, cmd.body, type=type, reply_to=reply_to)
        msg = maildir.deliver(meta, envelope)
        notes = msg["type"] + (", reply to #%s" % msg["reply_to"] if msg.get("reply_to") else "")
        self.say("sent #%s to %s (%s)" % (msg["id"], msg["to"], notes))


def run(meta, source, out=None, err=None, tick=TICK):
    repl = Repl(meta, source, out, err)
    repl.say(repl.banner(meta))
    try:
        while repl.tick(tick):
            pass
    except KeyboardInterrupt:
        # Ctrl-C is how people leave a REPL; it isn't a failure.
        repl.say("")
    return 0


def run_human(args):
    meta = session.resolve_args(args)
    if not session.is_active(meta):
        raise TandemError("session %s has ended" % meta["dir"])
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        raise TandemError("tandem human reads a terminal or a pipe on stdin")
    return run(meta, FdLines(fd))
