import json
import os
import sys
import time
from collections import namedtuple

from tandem import TandemError, cmdline, maildir, protocol, session, watch

# `event` is the harness event the hook is configured for. `safe` is what the hook prints when
# anything goes wrong; None prints nothing. A hook must never break the harness's loop, so every
# failure ends in `safe` and exit 0.
Hook = namedtuple("Hook", "name peer event run safe")

ERROR_CAP = 500
ASK = {"decision": "ask"}
NOOP = {}
REWAKE_PREFIX = "<task-notification>"
# A watcher that fails to start would otherwise make every idle Stop re-enter
# the loop to retry it.
WATCHDOG_BACKOFF = 60.0
# A module global rather than a parameter: hooks run from `tandem hook` through the CLI, so
# there is no caller to inject a clock, and tests patch this instead.
_clock = time.time


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
    return os.path.join(session.peer_dir(sdir, peer), "hooks.log")


def note_failure(sdir, name, peer, exc):
    """One line in `peers/<peer>/hooks.log`, or stderr when no session was found."""
    line = "%s %s: %s: %s" % (
        session.now_iso(),
        name,
        type(exc).__name__,
        str(exc).replace("\n", " ")[:ERROR_CAP],
    )
    if sdir is not None:
        try:
            path = hooks_log_path(sdir, peer)
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
            "ts": session.now_iso(),
            "from": "human",
            "to": protocol.peer_role(current, peer),
            "peer": peer,
            "body": body,
        }
        session.write_log_line(sdir, record)
    return True


def _is_rewake(prompt):
    # An asyncRewake wake reaches UserPromptSubmit as a <task-notification> block with the Stop
    # hook's stderr (the doorbell) in a <system-reminder>; it isn't the user typing (docs/probes
    # 2026-11-12 live check).
    return prompt.lstrip().startswith(REWAKE_PREFIX) and watch.DOORBELL_PREFIX in prompt


def claude_prompt(meta, payload):
    """UserPromptSubmit: log what the user typed. Prints nothing, since this hook's stdout
    would be added to the model's context."""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        raise HookInputError("no prompt string in stdin")
    if not prompt.strip() or prompt.startswith(protocol.LAUNCHER_PREFIX) or _is_rewake(prompt):
        return None
    log_console(meta, "claude", prompt)
    return None


def _agy_file(sdir, name):
    return os.path.join(session.peer_dir(sdir, "agy"), name)


def record_conversation(sdir, payload):
    """Keep `peers/agy/conversation_id` current, so the watcher rings the conversation agy is on.

    `run_hook` calls it before every agy hook, since each payload carries the id; an OSError
    there is logged, not raised, because the hook's own decision matters more.
    """
    conv = payload.get("conversationId")
    if not isinstance(conv, str) or not conv.strip():
        return
    conv = conv.strip()
    path = watch.conversation_path(sdir)
    current = session.read_text(path)
    if current is not None and current.strip() == conv:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    session.write_text_atomic(path, conv + "\n")


def _session_command(command):
    """`tandem <sub>` when `command` runs a tandem subcommand outside AGENT_COMMANDS, else None.

    Those start a harness or end or replace the session, which a navigator never does unasked.
    """
    for name, args in cmdline.commands(command):
        if name != "tandem":
            continue
        rest = list(args)
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] == "--session" else rest[1:]
        if rest and "tandem " + rest[0] not in protocol.AGENT_COMMANDS:
            return "tandem " + rest[0]
    return None


def agy_pretool(meta, payload):
    """PreToolUse for run_command: gate a navigator's writes, else pass through to agy's rules.

    Never returns `{}`, which agy reads as deny.
    """
    call = payload.get("toolCall")
    if not isinstance(call, dict):
        raise HookInputError("no toolCall object in stdin")
    if call.get("name") != "run_command":
        return ASK
    args = call.get("args")
    command = args.get("CommandLine") if isinstance(args, dict) else None
    if not isinstance(command, str):
        raise HookInputError("run_command without a CommandLine string")
    # Writing is the driver's job; any other role (or none) is gated.
    if session.role_of(meta, "agy") != "driver":
        reason = cmdline.write_reason(command) or _session_command(command)
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
    text = session.read_text(_agy_file(sdir, "watchdog"))
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


def agy_session_start(meta, payload):
    sdir = session.session_path(meta["dir"])
    if not session.is_active(meta):
        return NOOP
    role = protocol.peer_role(meta, "agy")
    lines = ["tandem: you are the %s in tandem session %s." % (role, meta["dir"])]
    if not watch.agy_watcher_alive(sdir, role, os.environ):
        lines.append("Your tandem watcher is not running. Start it now by running exactly: %s" % watch.detach_command(role))
        _mark_watchdog(sdir)
    pending = maildir.pending_counts(sdir)[role]
    if pending:
        lines.append("%d message(s) pending: run `tandem recv` until it says no mail." % pending)
        # Told here, so the watcher doesn't ring for the same mail.
        _claim_unrung(sdir, role)
    return _system_message(lines)


def agy_preinvocation(meta, payload):
    """PreInvocation: log what the user typed into agy, once.

    It fires before every model call of a turn with the same `lastUserInput`,
    so the last input seen is kept in `peers/agy/last_input`.
    """
    sdir = session.session_path(meta["dir"])
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
    if session.read_text(path) == text:
        return NOOP
    head = text.lstrip()
    if not (head.startswith(watch.DOORBELL_PREFIX) or head.startswith(protocol.LAUNCHER_PREFIX)):
        log_console(meta, "agy", text)
    # Written after logging: a failed log is retried on the next model call.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    session.write_text_atomic(path, text)
    return NOOP


def agy_stop(meta, payload):
    """Stop: when agy goes idle, restart a dead watcher and pick up mail no ring announced."""
    sdir = session.session_path(meta["dir"])
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


HOOKS = {
    hook.name: hook
    for hook in (
        Hook("claude-prompt", "claude", "UserPromptSubmit", claude_prompt, None),
        Hook("agy-pretool", "agy", "PreToolUse", agy_pretool, ASK),
        Hook("agy-session-start", "agy", "SessionStart", agy_session_start, NOOP),
        Hook("agy-preinvocation", "agy", "PreInvocation", agy_preinvocation, NOOP),
        Hook("agy-stop", "agy", "Stop", agy_stop, NOOP),
    )
}


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
        payload = parse_payload(raw)
        if hook.peer == "agy":
            try:
                record_conversation(sdir, payload)
            except OSError as exc:
                note_failure(sdir, hook.name, hook.peer, exc)
        output = hook.run(meta, payload)
    except Exception as exc:
        note_failure(sdir, hook.name, hook.peer, exc)
        output = hook.safe
    try:
        emit(output)
    except Exception:
        pass
    return 0
