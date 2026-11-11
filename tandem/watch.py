import argparse
import json
import math
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


def doorbell(role):
    return "tandem doorbell: new mail for %s. Run `tandem recv`." % role


def detach_command(role):
    """What agy runs to start its watcher: one plain command, so the bypass rule matches it."""
    return "tandem watch --as %s --detach" % role


def rung_path(sdir, role):
    return os.path.join(session.peer_dir(sdir, role), "rung")


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
    os.makedirs(session.peer_dir(sdir, role), exist_ok=True)
    session.write_text_atomic(rung_path(sdir, role), "%s\n" % maildir.format_id(msg_id))


def _write_pidfile(sdir, role, pid, ls_address):
    """The caller holds `session.locked(sdir)`."""
    os.makedirs(session.peer_dir(sdir, role), exist_ok=True)
    record = {"pid": pid, "ls_address": ls_address}
    session.write_text_atomic(session.watcher_pidfile(sdir, role), json.dumps(record) + "\n")


def _owner(sdir, role):
    return (session.read_watcher(sdir, role) or {}).get("pid")


def owns_pidfile(sdir, role, pid):
    return _owner(sdir, role) == pid


def superseded(sdir, role, pid):
    # Only a pidfile naming another pid supersedes: newer watchers replace the
    # file atomically, so a missing one means cleanup, not a successor.
    owner = _owner(sdir, role)
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


def wait_once(meta, role, deadline=math.inf, interval=POLL_INTERVAL, clock=time.monotonic, sleep=time.sleep, pid=None):
    """Claude's asyncRewake waiter: the doorbell text on mail, None otherwise.

    Returns None at `deadline` seconds, when the session ends, or once a newer
    waiter has taken the pidfile, which this one takes over from any older one.
    """
    sdir = session.session_path(meta["dir"])
    pid = os.getpid() if pid is None else pid
    end = clock() + deadline
    with session.locked(sdir):
        _write_pidfile(sdir, role, pid, None)
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
            left = end - clock()
            if left <= 0:
                return None
            sleep(min(interval, left))
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
    return environ["ANTIGRAVITY_LS_ADDRESS"]


def parse_ls_address(address):
    """(host, port) from `host:port`, `[v6]:port` or a URL such as `http://host:port/`; None if neither.

    The exact form agy uses is unverified (plan Task 17), so an optional scheme and path are
    accepted rather than read as a dead language server.
    """
    text = address.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    host, sep, port = text.rpartition(":")
    host = host.strip("[]")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        return None
    return host, int(port)


def ls_alive(address, timeout=2.0):
    """Whether agy's language server still accepts connections.

    A restarted or quit agy gets a new LS address, so a refused connect means
    the agy this watcher served is gone. An address this can't parse is never
    taken for a gone server; the watcher then stops only when superseded or
    when the session ends.
    """
    target = parse_ls_address(address)
    if target is None:
        return True
    try:
        socket.create_connection(target, timeout=timeout).close()
    except OSError:
        return False
    return True


def agentapi_exe(environ):
    return environ.get("ANTIGRAVITY_AGENTAPI_EXE") or "agy"


def conversation_path(sdir):
    return os.path.join(session.peer_dir(sdir, "agy"), "conversation_id")


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
    return os.path.join(session.peer_dir(sdir, role), "watch.log")


def live_watcher(sdir, role, ls_address=None):
    """Pid of a live agy watcher for `role` (serving the agy at `ls_address`, when given), or None.

    agy's hooks may run without the LS variables its tool calls get, so a
    live pid is all that can be checked then.
    """
    info = session.read_watcher(sdir, role)
    if info is None or not info.get("ls_address") or not session.pid_alive(info["pid"]):
        return None
    if ls_address and info["ls_address"] != ls_address:
        return None
    return info["pid"]


def agy_watcher_alive(sdir, role, environ):
    return live_watcher(sdir, role, environ.get("ANTIGRAVITY_LS_ADDRESS")) is not None


def claim_agy_pidfile(sdir, role, ls_address, pid):
    """Take the role's pidfile unless a live watcher serves the same agy; returns that one's pid.

    A pidfile naming another LS address, a dead pid or Claude's waiter is
    taken over; its owner, if alive, sees a foreign pid and exits.
    """
    with session.locked(sdir):
        running = live_watcher(sdir, role, ls_address)
        if running is not None and running != pid:
            return running
        _write_pidfile(sdir, role, pid, ls_address)
    return None


def _stamp(text):
    print("%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), text), flush=True)


def _ring(sdir, role, pid, msg_id, environ, log):
    """Ring agy for `msg_id`; True once it rang (rung marker advanced unless superseded)."""
    conv = conversation_id(sdir, environ)
    if conv is None:
        log("no conversation id yet for #%s; will retry" % maildir.format_id(msg_id))
        return False
    failure = send_doorbell(agentapi_exe(environ), conv, doorbell(role), environ)
    if failure is not None:
        log("send-message to %s for #%s failed: %s" % (conv, maildir.format_id(msg_id), failure))
        return False
    with session.locked(sdir):
        if not superseded(sdir, role, pid):
            mark_rung(sdir, role, msg_id)
    log("rang %s for #%s" % (conv, maildir.format_id(msg_id)))
    return True


def serve(
    meta,
    role,
    ls_address,
    environ=None,
    interval=POLL_INTERVAL,
    ls_interval=LS_CHECK_INTERVAL,
    retry_interval=RETRY_INTERVAL,
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
    pid = os.getpid() if pid is None else pid
    sdir = session.session_path(meta["dir"])

    running = claim_agy_pidfile(sdir, role, ls_address, pid)
    if running is not None:
        if ready:
            ready("running", running)
        return None
    session.log_event(sdir, "watcher_started", role=role, peer="agy", pid=pid)
    log("watching %s for %s (pid %d)" % (meta["dir"], role, pid))
    if parse_ls_address(ls_address) is None:
        log("cannot parse ANTIGRAVITY_LS_ADDRESS %r; not checking the language server" % ls_address)
    if ready:
        ready("started", pid)
    reason = "crashed"
    try:
        next_ls_check = clock()
        retry_at = None
        while True:
            if not _active(meta):
                reason = "session ended"
                return reason
            if superseded(sdir, role, pid):
                reason = "superseded"
                return reason
            now = clock()
            if now >= next_ls_check:
                if not ls_alive(ls_address):
                    reason = "agy language server gone"
                    return reason
                next_ls_check = now + ls_interval
            msg_id = unrung_id(sdir, role)
            if msg_id is not None and (retry_at is None or now >= retry_at):
                retry_at = None if _ring(sdir, role, pid, msg_id, environ, log) else clock() + retry_interval
            sleep(interval)
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
    deadline = session.seconds(args.deadline, "--deadline") if args.deadline is not None else math.inf
    meta = session.resolve_args(args)
    if not session.is_active(meta):
        return 0
    previous = signal.signal(signal.SIGTERM, _on_term)
    try:
        ring = wait_once(meta, role, deadline=deadline, interval=POLL_INTERVAL)
    finally:
        signal.signal(signal.SIGTERM, previous)
    if ring is None:
        return 0
    print(ring, file=sys.stderr)
    return MAIL


def _run_detach(args, role):
    interval = session.seconds(args.poll_interval, "--poll-interval", True) if args.poll_interval else POLL_INTERVAL
    ls_interval = (
        session.seconds(args.ls_check_interval, "--ls-check-interval", True) if args.ls_check_interval else LS_CHECK_INTERVAL
    )
    ls_address = agy_ls_address(os.environ)
    meta = session.resolve_args(args)
    if not session.is_active(meta):
        print("session %s has ended; no watcher started" % meta["dir"])
        return 0
    sdir = session.session_path(meta["dir"])

    # A watcher already running is found by the detached child's own pidfile claim, under the
    # lock, so a check here would only repeat it with a race.
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
    if role not in session.PAIR_ROLES:
        raise TandemError("--as must be one of %s for watch, not %r" % (", ".join(session.PAIR_ROLES), role))
    if args.deadline is not None and not args.once:
        raise TandemError("--deadline needs --once")
    if (args.poll_interval or args.ls_check_interval) and not args.detach:
        raise TandemError("--poll-interval and --ls-check-interval need --detach")
    if args.once:
        return _run_once(args, role)
    return _run_detach(args, role)
