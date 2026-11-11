import argparse
import sys
from collections import namedtuple

from tandem import TandemError, __version__, maildir, session

Command = namedtuple("Command", "name help configure run")

COMMANDS = []


def register(name, help, configure, run):
    """`configure(parser)` adds the command's arguments; `run(args)` returns an exit code."""
    if any(c.name == name for c in COMMANDS):
        raise ValueError("duplicate command: %s" % name)
    COMMANDS.append(Command(name, help, configure, run))


register(
    "init",
    "create a session for this repo and point the repo at it",
    session.configure_init,
    session.run_init,
)
register(
    "send",
    "deliver a message to a role's inbox and log it",
    maildir.configure_send,
    maildir.run_send,
)
register(
    "recv",
    "print the next message in a role's inbox and mark it read",
    maildir.configure_recv,
    maildir.run_recv,
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
    for cmd in COMMANDS:
        p = sub.add_parser(cmd.name, help=cmd.help, description=cmd.help)
        cmd.configure(p)
        p.set_defaults(_run=cmd.run)
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
