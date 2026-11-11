import argparse
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
import traceback

from tandem import TandemError, maildir, session

MAIL = 2
POLL_INTERVAL = 1.0
LS_CHECK_INTERVAL = 5.0
RETRY_INTERVAL = 5.0
SEND_TIMEOUT = 30.0
START_TIMEOUT = 10.0
AGY_ENV = ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN")
WATCHED_ROLES = maildir.WATCHED_ROLES


def doorbell(role):
    return "tandem doorbell: new mail for %s. Run `tandem recv`." % role


def detach_command(role):
    """What agy runs to start its watcher: one plain command, so the bypass rule matches it."""
    return "tandem watch --as %s --detach" % role


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


def agy_ls_address(environ):
    """The LS address of the agy process this command runs under; errors outside agy."""
    missing = [k for k in AGY_ENV if not environ.get(k)]
    if missing:
        raise TandemError(
            "%s not set: `tandem watch --detach` must be started by agy itself (a run_command tool "
            "call), since only agy's own children can reach its language server" % " and ".join(missing)
        )
    address = environ["ANTIGRAVITY_LS_ADDRESS"]
    parse_ls_address(address)
    return address


def parse_ls_address(address):
    host, sep, port = address.strip().rpartition(":")
    host = host.strip("[]")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise TandemError("ANTIGRAVITY_LS_ADDRESS is not host:port: %r" % address)
    return host, int(port)


def ls_alive(address, timeout=2.0):
    """Whether agy's language server still accepts connections.

    A restarted or quit agy gets a new LS address, so a refused connect means
    the agy this watcher served is gone.
    """
    try:
        socket.create_connection(parse_ls_address(address), timeout=timeout).close()
    except OSError:
        return False
    return True


def agentapi_exe(environ):
    return environ.get("ANTIGRAVITY_AGENTAPI_EXE") or "agy"


def conversation_path(sdir):
    return os.path.join(sdir, "peers", "agy", "conversation_id")


def conversation_id(sdir, environ):
    """agy's current conversation, as its hooks last wrote it.

    Read on every ring because `/new` changes it; the launch-time env value is
    only the fallback for before any hook has run.
    """
    try:
        with open(conversation_path(sdir), encoding="utf-8") as f:
            text = f.read().strip()
    except (OSError, UnicodeDecodeError):
        text = ""
    return text or environ.get("ANTIGRAVITY_CONVERSATION_ID") or None


def _reported_error(output):
    try:
        value = json.loads(output)
    except ValueError:
        return None
    if isinstance(value, dict) and "error" in value:
        return str(value["error"])
    return None


def _first_line(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:200]
    return ""


def send_doorbell(exe, conv, text, environ, run=subprocess.run, timeout=SEND_TIMEOUT):
    """Post `text` into agy conversation `conv`; None on success, else why it failed."""
    try:
        proc = run(
            [exe, "agentapi", "send-message", conv, text],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
            env=dict(environ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    # agentapi reports some failures as {"error": …}; trust that over the exit code.
    error = _reported_error(proc.stdout)
    if proc.returncode == 0 and error is None:
        return None
    return "exit %d: %s" % (proc.returncode, error or _first_line(proc.stderr) or _first_line(proc.stdout))


def watch_log_path(sdir, role):
    return os.path.join(peer_dir(sdir, role), "watch.log")


def watcher_info(sdir, role):
    """The pidfile as a dict (agy's `{pid, ls_address}` shape), or None."""
    try:
        with open(session.watcher_pidfile(sdir, role), encoding="utf-8") as f:
            value = json.loads(f.read())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def live_watcher(sdir, role, ls_address):
    """Pid of a live watcher already serving the agy at `ls_address`, or None."""
    info = watcher_info(sdir, role)
    if info is None or info.get("ls_address") != ls_address:
        return None
    pid = session.read_pidfile(session.watcher_pidfile(sdir, role))
    return pid if pid is not None and session.pid_alive(pid) else None


def agy_watcher_alive(sdir, role, environ):
    """Whether a live agy-shaped watcher serves `role`, and serves this agy when the LS is known.

    agy's hooks may run without the LS variables its tool calls get, so a
    live pid is all that can be checked then.
    """
    info = watcher_info(sdir, role)
    if info is None:
        return False
    pid = session.read_pidfile(session.watcher_pidfile(sdir, role))
    if pid is None or not session.pid_alive(pid):
        return False
    ls_address = environ.get("ANTIGRAVITY_LS_ADDRESS")
    return not ls_address or info.get("ls_address") == ls_address


def claim_agy_pidfile(sdir, role, ls_address, pid):
    """Take the role's pidfile unless a live watcher serves the same agy; returns that one's pid.

    A pidfile naming another LS address, a dead pid or Claude's bare-pid shape
    is taken over; its owner, if alive, sees a foreign pid and exits.
    """
    os.makedirs(peer_dir(sdir, role), exist_ok=True)
    with session.locked(sdir):
        running = live_watcher(sdir, role, ls_address)
        if running is not None and running != pid:
            return running
        record = {"pid": pid, "ls_address": ls_address}
        session.write_text_atomic(session.watcher_pidfile(sdir, role), json.dumps(record) + "\n")
    return None


def _stamp(text):
    print("%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), text), flush=True)


def _ring(sdir, role, pid, msg_id, environ, log):
    """Ring agy for `msg_id`; None on success (rung marker advanced), else why not."""
    conv = conversation_id(sdir, environ)
    if conv is None:
        log("no conversation id yet for #%s; will retry" % maildir.format_id(msg_id))
        return "no conversation id"
    failure = send_doorbell(agentapi_exe(environ), conv, doorbell(role), environ)
    if failure is not None:
        log("send-message to %s for #%s failed: %s" % (conv, maildir.format_id(msg_id), failure))
        return failure
    with session.locked(sdir):
        if not superseded(sdir, role, pid):
            mark_rung(sdir, role, msg_id)
    log("rang %s for #%s" % (conv, maildir.format_id(msg_id)))
    return None


def _loop(meta, sdir, role, ls_address, environ, interval, ls_interval, retry_interval, clock, sleep, pid, log):
    next_ls_check = clock()
    retry_at = None
    while True:
        if not _active(meta):
            return "session ended"
        if superseded(sdir, role, pid):
            return "superseded"
        now = clock()
        if now >= next_ls_check:
            if not ls_alive(ls_address):
                return "agy language server gone"
            next_ls_check = now + ls_interval
        msg_id = unrung_id(sdir, role)
        if msg_id is not None and (retry_at is None or now >= retry_at):
            failed = _ring(sdir, role, pid, msg_id, environ, log)
            retry_at = None if failed is None else clock() + retry_interval
        sleep(interval)


def serve(
    meta,
    role,
    ls_address,
    environ=None,
    interval=None,
    ls_interval=None,
    retry_interval=None,
    clock=time.monotonic,
    sleep=time.sleep,
    pid=None,
    log=_stamp,
    ready=None,
):
    """agy's watcher loop: ring the doorbell through agentapi until told to stop.

    Returns why it stopped, or None when a live watcher already serves this
    agy. `ready(status, pid)` is called once the pidfile question is settled,
    with "started" and this pid or "running" and the other watcher's.
    """
    environ = os.environ if environ is None else environ
    interval = POLL_INTERVAL if interval is None else interval
    ls_interval = LS_CHECK_INTERVAL if ls_interval is None else ls_interval
    retry_interval = RETRY_INTERVAL if retry_interval is None else retry_interval
    pid = os.getpid() if pid is None else pid
    sdir = session.session_path(meta["dir"])

    running = claim_agy_pidfile(sdir, role, ls_address, pid)
    if running is not None:
        if ready:
            ready("running", running)
        return None
    session.log_event(sdir, "watcher_started", role=role, peer="agy", pid=pid)
    log("watching %s for %s (pid %d)" % (meta["dir"], role, pid))
    if ready:
        ready("started", pid)
    reason = "crashed"
    try:
        reason = _loop(meta, sdir, role, ls_address, environ, interval, ls_interval, retry_interval, clock, sleep, pid, log)
        return reason
    except SystemExit:
        reason = "terminated"
        raise
    finally:
        release_pidfile(sdir, role, pid)
        log("exiting: %s" % reason)
        if os.path.isdir(sdir):
            session.log_event(sdir, "watcher_exited", role=role, peer="agy", pid=pid, reason=reason)


def _redirect_stdio(log_path):
    devnull = os.open(os.devnull, os.O_RDONLY)
    out = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(devnull, 0)
    os.dup2(out, 1)
    os.dup2(out, 2)
    os.close(devnull)
    os.close(out)


def detach(log_path, start, timeout=START_TIMEOUT):
    """Run `start(notify)` in a grandchild in its own session, stdio on `log_path`.

    The double fork and setsid take the watcher out of agy's tool call: the
    call returns once this parent exits, and the watcher can never regain a
    controlling terminal. Returns the text the grandchild passed to `notify`,
    or "" if it died or stayed silent past `timeout`.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    r, w = os.pipe()
    child = os.fork()
    if child == 0:
        code = 1
        try:
            os.close(r)
            os.setsid()
            if os.fork() != 0:
                os._exit(0)
            _redirect_stdio(log_path)

            def notify(text):
                os.write(w, text.encode("utf-8"))
                os.close(w)

            code = start(notify)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
        except BaseException:
            traceback.print_exc()
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code)
    os.close(w)
    os.waitpid(child, 0)
    chunks = []
    end = time.monotonic() + timeout
    try:
        while True:
            left = end - time.monotonic()
            if left <= 0 or not select.select([r], [], [], left)[0]:
                break
            data = os.read(r, 4096)
            if not data:
                break
            chunks.append(data)
    finally:
        os.close(r)
    return b"".join(chunks).decode("utf-8", "replace")


def _seconds(text, flag, positive=False):
    try:
        value = float(text)
    except ValueError:
        raise TandemError("%s must be a number of seconds, not %r" % (flag, text))
    if value != value or value < 0 or (positive and value == 0):
        raise TandemError("%s must %s" % (flag, "be positive" if positive else "not be negative"))
    return value


def _on_term(signum, frame):
    # Claude kills the waiter at the hook timeout, and a detached watcher is
    # only ever stopped by a signal; exiting through SystemExit runs the
    # finally that releases the pidfile and logs the exit.
    raise SystemExit(0)


def configure_watch(p):
    p.add_argument("--as", dest="as_role", metavar="ROLE", help="whose inbox to watch (default: $TANDEM_ROLE)")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="exit 2 with the doorbell on stderr when mail arrives (Claude)")
    mode.add_argument(
        "--detach", action="store_true", help="ring agy through agentapi from a background watcher (agy runs this)"
    )
    p.add_argument("--deadline", metavar="S", help="with --once, exit 0 after S seconds without mail")
    # Test knobs: the detached watcher is a separate process, so tests can't patch its intervals.
    p.add_argument("--poll-interval", metavar="S", help=argparse.SUPPRESS)
    p.add_argument("--ls-check-interval", metavar="S", help=argparse.SUPPRESS)


def _run_once(args, role):
    deadline = _seconds(args.deadline, "--deadline") if args.deadline is not None else None
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


def _run_detach(args, role):
    interval = _seconds(args.poll_interval, "--poll-interval", True) if args.poll_interval else None
    ls_interval = _seconds(args.ls_check_interval, "--ls-check-interval", True) if args.ls_check_interval else None
    ls_address = agy_ls_address(os.environ)
    meta = session.resolve_args(args)
    if not session.is_active(meta):
        print("session %s has ended; no watcher started" % meta["dir"])
        return 0
    sdir = session.session_path(meta["dir"])
    running = live_watcher(sdir, role, ls_address)
    if running is not None:
        print("tandem watch: already running for %s (pid %d)" % (role, running))
        return 0

    def start(notify):
        signal.signal(signal.SIGTERM, _on_term)
        serve(
            meta,
            role,
            ls_address,
            interval=interval,
            ls_interval=ls_interval,
            ready=lambda status, pid: notify("%s %d" % (status, pid)),
        )
        return 0

    log_path = watch_log_path(sdir, role)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    status, _, pid = detach(log_path, start).partition(" ")
    if status == "started":
        print("tandem watch: started for %s (pid %s), log %s" % (role, pid, log_path))
        return 0
    if status == "running":
        print("tandem watch: already running for %s (pid %s)" % (role, pid))
        return 0
    raise TandemError("watcher did not start; see %s" % log_path)


def run_watch(args):
    role = maildir.own_role(args, "watcher")
    if role not in WATCHED_ROLES:
        raise TandemError("--as must be one of %s for watch, not %r" % (", ".join(WATCHED_ROLES), role))
    if args.deadline is not None and not args.once:
        raise TandemError("--deadline needs --once")
    if (args.poll_interval or args.ls_check_interval) and not args.detach:
        raise TandemError("--poll-interval and --ls-check-interval need --detach")
    if args.once:
        return _run_once(args, role)
    return _run_detach(args, role)
