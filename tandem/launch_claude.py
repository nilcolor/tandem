import argparse
import os

from tandem import TandemError, hooks, paths, protocol, session

PEER = "claude"
# The waiter's deadline sits just under the hook timeout so it exits 0 on its
# own instead of being killed by Claude.
STOP_TIMEOUT = 14400
WAIT_DEADLINE = 14340
ALLOW = ["Bash(tandem *)"]
# A bare tool name in deny removes the tool from Claude's context entirely.
NAVIGATOR_DENY = ["Edit", "Write", "NotebookEdit"]


def peer_dir(sdir):
    return os.path.join(sdir, "peers", PEER)


def settings_path(sdir):
    return os.path.join(peer_dir(sdir), "settings.json")


def protocol_path(sdir):
    return os.path.join(peer_dir(sdir), "protocol.md")


def build_settings(meta, role):
    dir_name = meta["dir"]
    waiter = paths.shim_command(
        "--session", dir_name, "watch", "--as", role, "--once", "--deadline", str(WAIT_DEADLINE)
    )
    prompt_log = paths.shim_command("--session", dir_name, "hook", "claude-prompt")
    permissions = {"allow": list(ALLOW)}
    if role == "navigator":
        permissions["deny"] = list(NAVIGATOR_DENY)
    return {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "asyncRewake": True, "timeout": STOP_TIMEOUT, "command": waiter}]}
            ],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": prompt_log}]}],
        },
        "permissions": permissions,
    }


def initial_prompt(meta, role):
    name = meta.get("name") or session.short_name(meta["dir"])
    if role == "driver":
        then = "then start the intake phase as your tandem protocol describes"
    else:
        then = "then wait for the driver's plan: if there was no mail, say so and end your turn"
    return "%s session `%s` is up and you are the %s. Run `tandem recv` until it says no mail, %s." % (
        hooks.LAUNCHER_PREFIX,
        name,
        role,
        then,
    )


def prepare(meta, role=None, extra=(), pid=None, environ=None):
    """Claim the role, write the session's Claude config; returns (argv, env, cwd) to exec."""
    meta = session.claim_role(meta["dir"], PEER, role, pid=pid)
    role = protocol.peer_role(meta, PEER)
    sdir = session.session_path(meta["dir"])
    os.makedirs(peer_dir(sdir), exist_ok=True)
    session.write_json_atomic(settings_path(sdir), build_settings(meta, role))
    text = protocol.render(meta, PEER, role)
    session.write_text_atomic(protocol_path(sdir), text)

    # --add-dir lets Claude read (and, as driver, write) plan.md, decisions.md and reviews
    # in the session dir without a prompt. It takes several values, so it is followed by
    # another option, and `--` keeps a variadic option in `extra` from eating the prompt.
    argv = ["claude", "--add-dir", sdir, "--settings", settings_path(sdir), "--append-system-prompt", text]
    argv += list(extra)
    argv += ["--", initial_prompt(meta, role)]
    env = dict(os.environ if environ is None else environ)
    env["TANDEM_SESSION"] = meta["dir"]
    env["TANDEM_ROLE"] = role
    return argv, env, meta["repo"]


def configure_claude(p):
    p.add_argument("--role", choices=session.PAIR_ROLES, help="role to claim (default: the one session.json gives claude)")
    p.add_argument("extra", nargs=argparse.REMAINDER, help="after --: arguments passed to claude as they are")


def _extra(args):
    extra = list(args.extra or [])
    if extra and extra[0] == "--":
        extra = extra[1:]
    elif extra:
        raise TandemError("pass claude's own arguments after --, e.g. `tandem claude -- --model opus`")
    return extra


def run_claude(args, exec_fn=None):
    extra = _extra(args)
    meta = session.resolve_args(args)
    argv, env, cwd = prepare(meta, args.role, extra)
    (exec_fn or session.exec_harness)(argv, env, cwd)
    return 0
