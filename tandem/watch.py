import os
import signal
import sys
import time

from tandem import TandemError, maildir, session

MAIL = 2
POLL_INTERVAL = 1.0
WATCHED_ROLES = maildir.WATCHED_ROLES


def doorbell(role):
    return "tandem doorbell: new mail for %s. Run `tandem recv`." % role


def peer_dir(sdir, role):
    return os.path.join(sdir, "peers", role)


def rung_path(sdir, role):
    return os.path.join(peer_dir(sdir, role), "rung")


def read_rung(sdir, role):
    """Highest message id already rung for `role`; 0 when nothing has been rung."""
    try:
        with open(rung_path(sdir, role), encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return 0
    return int(text) if text.isdigit() else 0


def unrung_id(sdir, role):
    """Highest unread message id above the rung marker, or None.

    Ringing for the highest id covers every message below it: one ring per
    batch, and a message the agent leaves unread isn't rung again.
    """
    names = maildir._inbox_names(sdir, role, "new")
    if not names:
        return None
    newest = int(names[-1][: -len(".json")])
    return newest if newest > read_rung(sdir, role) else None


def mark_rung(sdir, role, msg_id):
    """Advance the rung marker to `msg_id`; the caller holds `session.locked(sdir)`.

    Never moves it back, so a slow ringer can't make a newer message ring twice.
    """
    if msg_id <= read_rung(sdir, role):
        return
    os.makedirs(peer_dir(sdir, role), exist_ok=True)
    session.write_text_atomic(rung_path(sdir, role), "%s\n" % maildir.format_id(msg_id))


def claim_pidfile(sdir, role, text):
    """Make this process the role's watcher; any older one sees a foreign pid and exits."""
    os.makedirs(peer_dir(sdir, role), exist_ok=True)
    with session.locked(sdir):
        session.write_text_atomic(session.watcher_pidfile(sdir, role), text)


def owns_pidfile(sdir, role, pid):
    return session.read_pidfile(session.watcher_pidfile(sdir, role)) == pid


def superseded(sdir, role, pid):
    # Only a pidfile naming another pid supersedes: newer watchers replace the
    # file atomically, so a missing one means cleanup, not a successor.
    owner = session.read_pidfile(session.watcher_pidfile(sdir, role))
    return owner is not None and owner != pid


def release_pidfile(sdir, role, pid):
    """Remove the pidfile if it still names `pid`, so status reads "not running"."""
    if not os.path.isdir(sdir):
        return
    with session.locked(sdir):
        if owns_pidfile(sdir, role, pid):
            try:
                os.unlink(session.watcher_pidfile(sdir, role))
            except FileNotFoundError:
                pass


def _active(meta):
    try:
        return session.is_active(session.load_session(meta["dir"]))
    except TandemError:
        return False


def wait_once(meta, role, deadline=None, interval=None, clock=time.monotonic, sleep=time.sleep, pid=None):
    """Claude's asyncRewake waiter: the doorbell text on mail, None otherwise.

    Returns None at `deadline` seconds, when the session ends, or once a newer
    waiter has taken the pidfile.
    """
    sdir = session.session_path(meta["dir"])
    pid = os.getpid() if pid is None else pid
    interval = POLL_INTERVAL if interval is None else interval
    end = None if deadline is None else clock() + deadline
    claim_pidfile(sdir, role, "%d\n" % pid)
    try:
        while True:
            if not _active(meta) or superseded(sdir, role, pid):
                return None
            msg_id = unrung_id(sdir, role)
            if msg_id is not None:
                with session.locked(sdir):
                    # Re-checked under the lock: a waiter superseded since the
                    # check above must leave the ring to its successor.
                    if not superseded(sdir, role, pid) and unrung_id(sdir, role) == msg_id:
                        mark_rung(sdir, role, msg_id)
                        return doorbell(role)
                continue
            if end is not None:
                left = end - clock()
                if left <= 0:
                    return None
                sleep(min(interval, left))
            else:
                sleep(interval)
    finally:
        release_pidfile(sdir, role, pid)


def _seconds(text):
    try:
        value = float(text)
    except ValueError:
        raise TandemError("--deadline must be a number of seconds, not %r" % text)
    if value < 0 or value != value:
        raise TandemError("--deadline must not be negative")
    return value


def _on_term(signum, frame):
    # Claude kills the waiter at the hook timeout; exiting through SystemExit
    # runs the finally that releases the pidfile.
    raise SystemExit(0)


def configure_watch(p):
    p.add_argument("--as", dest="as_role", metavar="ROLE", help="whose inbox to watch (default: $TANDEM_ROLE)")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="exit 2 with the doorbell on stderr when mail arrives (Claude)")
    p.add_argument("--deadline", metavar="S", help="with --once, exit 0 after S seconds without mail")


def run_watch(args):
    role = maildir.own_role(args, "watcher")
    if role not in WATCHED_ROLES:
        raise TandemError("--as must be one of %s for watch, not %r" % (", ".join(WATCHED_ROLES), role))
    if args.deadline is not None and not args.once:
        raise TandemError("--deadline needs --once")
    deadline = _seconds(args.deadline) if args.deadline is not None else None
    meta = session.resolve_args(args)
    if not session.is_active(meta):
        return 0
    previous = signal.signal(signal.SIGTERM, _on_term)
    try:
        ring = wait_once(meta, role, deadline=deadline)
    finally:
        signal.signal(signal.SIGTERM, previous)
    if ring is None:
        return 0
    print(ring, file=sys.stderr)
    return MAIL
