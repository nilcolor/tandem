import json
import os
import sys
from collections import namedtuple

from tandem import TandemError, protocol, session

# `safe` is what the hook prints when anything goes wrong; None prints nothing.
# A hook must never break the harness's loop, so every failure ends in `safe`
# and exit 0.
Hook = namedtuple("Hook", "name peer run safe")

HOOKS = {}

# The launcher's initial prompt carries this prefix so it isn't logged as human input.
LAUNCHER_PREFIX = "tandem:"
ERROR_CAP = 500


def register(name, peer, safe):
    def wrap(run):
        if name in HOOKS:
            raise ValueError("duplicate hook: %s" % name)
        HOOKS[name] = Hook(name, peer, run, safe)
        return run

    return wrap


class HookInputError(TandemError):
    """The harness sent stdin this hook can't use."""


def read_stdin(stream=None):
    stream = stream or sys.stdin
    raw = getattr(stream, "buffer", stream).read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return raw


def parse_payload(raw):
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise HookInputError("stdin is not JSON: %s" % exc)
    if not isinstance(payload, dict):
        raise HookInputError("stdin is not a JSON object")
    return payload


def hooks_log_path(sdir, peer):
    return os.path.join(sdir, "peers", peer, "hooks.log")


def note_failure(sdir, hook, exc):
    """One line in `peers/<peer>/hooks.log`, or stderr when no session was found."""
    line = "%s %s: %s: %s" % (
        session.iso(session._now()),
        hook.name,
        type(exc).__name__,
        str(exc).replace("\n", " ")[:ERROR_CAP],
    )
    if sdir is not None:
        try:
            path = hooks_log_path(sdir, hook.peer)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return
        except OSError:
            pass
    try:
        print("tandem: %s" % line, file=sys.stderr)
    except Exception:
        pass


def log_console(meta, peer, body):
    """Append a `console` line for input typed into `peer`'s console; False if the session ended.

    The role is read under the lock, so a role claimed after this hook's
    process started is the one recorded.
    """
    sdir = session.session_path(meta["dir"])
    with session.locked(sdir):
        current = session.load_session(meta["dir"])
        if not session.is_active(current):
            return False
        record = {
            "kind": "console",
            "ts": session.iso(session._now()),
            "from": "human",
            "to": protocol.peer_role(current, peer),
            "peer": peer,
            "body": body,
        }
        session.write_log_line(sdir, record)
    return True


@register("claude-prompt", "claude", safe=None)
def claude_prompt(meta, payload):
    """UserPromptSubmit: log what the user typed. Prints nothing, since this hook's stdout
    would be added to the model's context."""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        raise HookInputError("no prompt string in stdin")
    if not prompt.strip() or prompt.startswith(LAUNCHER_PREFIX):
        return None
    log_console(meta, "claude", prompt)
    return None


def emit(output):
    if output is None:
        return
    sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def configure_hook(p):
    p.add_argument("event", choices=sorted(HOOKS), help="the hook being run")


def run_hook(args):
    hook = HOOKS[args.event]
    sdir = None
    output = hook.safe
    try:
        # Drained before resolving, so the harness never writes into a closed pipe.
        raw = read_stdin()
        meta = session.resolve_args(args)
        sdir = session.session_path(meta["dir"])
        output = hook.run(meta, parse_payload(raw))
    except Exception as exc:
        note_failure(sdir, hook, exc)
        output = hook.safe
    try:
        emit(output)
    except Exception:
        pass
    return 0
