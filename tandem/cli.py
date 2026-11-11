import argparse
import sys

from tandem import TandemError, __version__, hooks, human, launch_agy, launch_claude, maildir, session, watch

# (name, help, configure(parser) or None, run(args) → exit code)
COMMANDS = (
    ("init", "create a session for this repo and point the repo at it", session.configure_init, session.run_init),
    ("send", "deliver a message to a role's inbox and log it", maildir.configure_send, maildir.run_send),
    ("recv", "print the next message in a role's inbox and mark it read", maildir.configure_recv, maildir.run_recv),
    ("watch", "wait for mail and ring the doorbell", watch.configure_watch, watch.run_watch),
    ("log", "print the session log (messages, console input, events)", maildir.configure_log, maildir.run_log),
    (
        "status",
        "show active sessions, pending mail, last messages, watchers and stale pointers",
        None,
        maildir.run_status,
    ),
    ("done", "write summary.md, end the session and clear the repo pointer", None, session.run_done),
    (
        "claude",
        "launch Claude Code as this session's driver or navigator (execs claude)",
        launch_claude.configure_claude,
        launch_claude.run_claude,
    ),
    (
        "agy",
        "launch agy as this session's navigator or driver (checks the sandbox profile, execs agy)",
        launch_agy.configure_agy,
        launch_agy.run_agy,
    ),
    ("human", "read the human inbox and send to the driver or navigator (REPL)", None, human.run_human),
    (
        "hook",
        "internal: run a harness hook (stdin JSON in, JSON or nothing out, always exit 0)",
        hooks.configure_hook,
        hooks.run_hook,
    ),
)


class _Parser(argparse.ArgumentParser):
    # argparse exits 2 on usage errors, but 2 means "mail" for `watch --once`
    # (Claude's asyncRewake wake), so usage errors must exit 1 like any other.
    def error(self, message):
        self.exit(1, "tandem: %s\n" % message)


def build_parser():
    parser = _Parser(prog="tandem", description="Two coding agents, one task, one bus.")
    parser.add_argument("--version", action="version", version="tandem %s" % __version__)
    parser.add_argument("--session", metavar="NAME", help="session dir name or short name")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True
    for name, text, configure, run in COMMANDS:
        p = sub.add_parser(name, help=text, description=text)
        if configure is not None:
            configure(p)
        p.set_defaults(_run=run)
    return parser


def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code is None or isinstance(exc.code, int):
            return exc.code or 0
        return 1
    try:
        return args._run(args) or 0
    except TandemError as exc:
        print("tandem: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Ctrl-C in a blocking `recv --wait`; the shell convention for SIGINT, without a traceback.
        print(file=sys.stderr)
        return 130
