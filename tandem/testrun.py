"""`tandem test <command>`: run a test command in the repo under the session's test lock."""

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys

from tandem import TandemError, session

# Forwarded to the test process, so a harness killing a slow command doesn't leave the tests
# running after the lock is gone.
FORWARDED = (signal.SIGTERM, signal.SIGHUP)


def lock_path(sdir):
    return os.path.join(sdir, "test.lock")


def holder_path(sdir):
    return os.path.join(sdir, "test.holder")


def describe_holder(sdir):
    """Who holds the test lock, from `test.holder`; a generic phrase when it can't be read."""
    try:
        holder = json.loads(session.read_text(holder_path(sdir)) or "")
        return "the %s's `%s` (pid %d, since %s)" % (holder["role"], holder["command"], holder["pid"], holder["since"])
    except (ValueError, KeyError, TypeError):
        return "another test run"


def _acquire(fd, sdir, err):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("tandem: waiting for the test lock, held by %s" % describe_holder(sdir), file=err)
        err.flush()
        fcntl.flock(fd, fcntl.LOCK_EX)


def _run(argv, cwd):
    """`argv`'s exit code, shell style (128 + signal), forwarding FORWARDED signals to it."""
    started = []

    def forward(signum, frame):
        if not started:
            raise SystemExit(128 + signum)
        started[0].send_signal(signum)

    # Installed before the child starts, so no signal lands in between and kills tandem alone.
    saved = {sig: signal.signal(sig, forward) for sig in FORWARDED}
    try:
        try:
            child = subprocess.Popen(argv, cwd=cwd)
        except OSError as exc:
            raise TandemError("cannot run %s: %s" % (argv[0], exc))
        started.append(child)
        interrupts = 0
        while True:
            try:
                code = child.wait()
                break
            except KeyboardInterrupt:
                # Ctrl-C reached the child too, through the terminal's process group: let it stop
                # on its own, and kill it only on a second Ctrl-C.
                interrupts += 1
                if interrupts > 1:
                    child.kill()
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)
    return code if code >= 0 else 128 - code


def run_locked(meta, argv, role=None, err=None):
    """Run `argv` in the session's repo once the test lock is free; returns its exit code.

    tandem stays the parent and holds the lock itself: handing the lock fd to the test process
    would let a daemon it spawns (spring, a compose service) keep the lock after the run ends.
    """
    err = err or sys.stderr
    sdir = session.session_path(meta["dir"])
    fd = os.open(lock_path(sdir), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _acquire(fd, sdir, err)
        holder = {"role": role or "user", "command": " ".join(argv), "pid": os.getpid(), "since": session.now_iso()}
        session.write_json_atomic(holder_path(sdir), holder)
        try:
            return _run(argv, meta["repo"])
        finally:
            try:
                os.remove(holder_path(sdir))
            except OSError:
                pass
    finally:
        os.close(fd)


def configure_test(p):
    p.add_argument("command", nargs=argparse.REMAINDER, help="the test command and its arguments")


def run_test(args):
    argv = list(args.command)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise TandemError("nothing to run: tandem test <command> [args]")
    meta = session.resolve_args(args)
    if not session.is_active(meta):
        raise TandemError("session %s has ended" % meta["dir"])
    return run_locked(meta, argv, os.environ.get("TANDEM_ROLE"))
