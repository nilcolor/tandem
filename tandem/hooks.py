import json
import os
import sys
import time
from collections import namedtuple

from tandem import TandemError, cmdline, maildir, protocol, session, watch

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


ASK = {"decision": "ask"}
BYPASS = {"decision": "allow", "overwrite": {"BypassSandbox": True}}
NOOP = {}
DOORBELL_PREFIX = "tandem doorbell"
# A watcher that fails to start would otherwise make every idle Stop re-enter
# the loop to retry it.
WATCHDOG_BACKOFF = 60.0
_clock = time.time


def _agy_file(sdir, name):
    return os.path.join(sdir, "peers", "agy", name)


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def record_conversation(sdir, payload, hook_name):
    """Keep `peers/agy/conversation_id` current, so the watcher rings the conversation agy is on.

    A failure here is logged, not raised: the hook's own decision matters more.
    """
    conv = payload.get("conversationId")
    if not isinstance(conv, str) or not conv.strip():
        return
    conv = conv.strip()
    path = watch.conversation_path(sdir)
    current = _read_text(path)
    if current is not None and current.strip() == conv:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        session.write_text_atomic(path, conv + "\n")
    except OSError as exc:
        note_failure(sdir, HOOKS[hook_name], exc)


def bypass_prefixes(meta):
    cmds = meta.get("test_cmds") or []
    return [c for c in cmds if isinstance(c, str) and c.strip()] + ["tandem"]


@register("agy-pretool", "agy", safe=ASK)
def agy_pretool(meta, payload):
    """PreToolUse for run_command: force the bypass, gate a navigator's writes, or pass through.

    Never returns `{}`, which agy reads as deny.
    """
    record_conversation(session.session_path(meta["dir"]), payload, "agy-pretool")
    call = payload.get("toolCall")
    if not isinstance(call, dict):
        raise HookInputError("no toolCall object in stdin")
    if call.get("name") != "run_command":
        return ASK
    args = call.get("args")
    command = args.get("CommandLine") if isinstance(args, dict) else None
    if not isinstance(command, str):
        raise HookInputError("run_command without a CommandLine string")
    if cmdline.bypass_prefix(command, bypass_prefixes(meta)) is not None:
        return BYPASS
    # Writing is the driver's job; any other role (or none) is gated.
    if session.role_of(meta, "agy") != "driver":
        reason = cmdline.write_reason(command)
        if reason is not None:
            return {
                "decision": "force_ask",
                "reason": "tandem write gate: this looks like a write (%s), and the navigator "
                "doesn't change files" % reason,
            }
    return ASK


def _system_message(lines):
    return {"injectSteps": [{"systemMessage": {"systemMessage": "\n".join(lines)}}]}


def _watchdog_due(sdir):
    text = _read_text(_agy_file(sdir, "watchdog"))
    try:
        last = float(text) if text else None
    except ValueError:
        last = None
    now = _clock()
    return last is None or not 0 <= now - last < WATCHDOG_BACKOFF


def _mark_watchdog(sdir):
    os.makedirs(os.path.dirname(_agy_file(sdir, "watchdog")), exist_ok=True)
    session.write_text_atomic(_agy_file(sdir, "watchdog"), "%.3f\n" % _clock())


def _claim_unrung(sdir, role):
    """Mark the newest unrung message rung; returns its id, or None when nothing is unrung."""
    with session.locked(sdir):
        msg_id = watch.unrung_id(sdir, role)
        if msg_id is not None:
            watch.mark_rung(sdir, role, msg_id)
    return msg_id


@register("agy-session-start", "agy", safe=NOOP)
def agy_session_start(meta, payload):
    sdir = session.session_path(meta["dir"])
    record_conversation(sdir, payload, "agy-session-start")
    if not session.is_active(meta):
        return NOOP
    role = protocol.peer_role(meta, "agy")
    lines = ["tandem: you are the %s in tandem session %s." % (role, meta["dir"])]
    if not watch.agy_watcher_alive(sdir, role, os.environ):
        lines.append("Your tandem watcher is not running. Start it now by running exactly: %s" % watch.detach_command(role))
        _mark_watchdog(sdir)
    pending = maildir.pending_counts(sdir).get(role, 0)
    if pending:
        lines.append("%d message(s) pending: run `tandem recv` until it says no mail." % pending)
        # Told here, so the watcher doesn't ring for the same mail.
        _claim_unrung(sdir, role)
    return _system_message(lines)


@register("agy-preinvocation", "agy", safe=NOOP)
def agy_preinvocation(meta, payload):
    """PreInvocation: log what the user typed into agy, once.

    It fires before every model call of a turn with the same `lastUserInput`,
    so the last input seen is kept in `peers/agy/last_input`.
    """
    sdir = session.session_path(meta["dir"])
    record_conversation(sdir, payload, "agy-preinvocation")
    if not session.is_active(meta):
        return NOOP
    text = payload.get("lastUserInput")
    if text is None:
        return NOOP
    if not isinstance(text, str):
        raise HookInputError("lastUserInput is not a string")
    if not text.strip():
        return NOOP
    path = _agy_file(sdir, "last_input")
    if _read_text(path) == text:
        return NOOP
    head = text.lstrip()
    if not (head.startswith(DOORBELL_PREFIX) or head.startswith(LAUNCHER_PREFIX)):
        log_console(meta, "agy", text)
    # Written after logging: a failed log is retried on the next model call.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    session.write_text_atomic(path, text)
    return NOOP


@register("agy-stop", "agy", safe=NOOP)
def agy_stop(meta, payload):
    """Stop: when agy goes idle, restart a dead watcher and pick up mail no ring announced."""
    sdir = session.session_path(meta["dir"])
    record_conversation(sdir, payload, "agy-stop")
    if not session.is_active(meta):
        return NOOP
    if payload.get("terminationReason") != "NO_TOOL_CALL" or payload.get("fullyIdle") is not True:
        return NOOP
    role = protocol.peer_role(meta, "agy")
    reasons = []
    if not watch.agy_watcher_alive(sdir, role, os.environ) and _watchdog_due(sdir):
        reasons.append(
            "tandem: your watcher is not running, so you won't hear the doorbell. Start it by running "
            "exactly: %s" % watch.detach_command(role)
        )
        _mark_watchdog(sdir)
    if _claim_unrung(sdir, role) is not None:
        reasons.append("tandem: new mail for %s. Run `tandem recv` until it says no mail." % role)
    if not reasons:
        return NOOP
    return {"decision": "continue", "reason": "\n".join(reasons)}


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
