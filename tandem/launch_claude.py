import argparse
import os

from tandem import paths, protocol, session

PEER = "claude"
# The waiter's deadline sits just under the hook timeout so it exits 0 on its
# own instead of being killed by Claude.
STOP_TIMEOUT = 14400
WAIT_DEADLINE = STOP_TIMEOUT - 60
# A bare tool name in deny removes the tool from Claude's context entirely.
NAVIGATOR_DENY = ["Edit", "Write", "NotebookEdit"]


def settings_path(sdir):
    return os.path.join(session.peer_dir(sdir, PEER), "settings.json")


def protocol_path(sdir):
    return os.path.join(session.peer_dir(sdir, PEER), "protocol.md")


def build_settings(meta, role):
    dir_name = meta["dir"]
    waiter = paths.shim_command(
        "--session", dir_name, "watch", "--as", role, "--once", "--deadline", str(WAIT_DEADLINE)
    )
    prompt_log = paths.shim_command("--session", dir_name, "hook", "claude-prompt")
    # `Bash(<prefix> *)` also matches the bare prefix, and the space keeps `tandem sendx` out.
    permissions = {"allow": ["Bash(%s *)" % prefix for prefix in protocol.approved_prefixes(meta, role)]}
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


def prepare(meta, role=None, extra=()):
    """Claim the role, write the session's Claude config; returns (argv, env, cwd) to exec."""
    meta = session.claim_role(meta["dir"], PEER, role)
    role = protocol.peer_role(meta, PEER)
    sdir = session.session_path(meta["dir"])
    os.makedirs(session.peer_dir(sdir, PEER), exist_ok=True)
    session.write_json_atomic(settings_path(sdir), build_settings(meta, role))
    text = protocol.render(meta, PEER, role)
    session.write_text_atomic(protocol_path(sdir), text)
    protocol.write_guides(sdir)

    # --add-dir lets Claude read (and, as driver, write) plan.md, decisions.md and reviews
    # in the session dir without a prompt. It takes several values, so it is followed by
    # another option, and `--` keeps a variadic option in `extra` from eating the prompt.
    argv = ["claude", "--add-dir", sdir, "--settings", settings_path(sdir), "--append-system-prompt", text]
    argv += list(extra)
    argv += ["--", protocol.initial_prompt(meta, role)]
    return argv, session.harness_env(meta, role), meta["repo"]


def configure_claude(p):
    p.add_argument("--role", choices=session.PAIR_ROLES, help="role to claim (default: the one session.json gives claude)")
    p.add_argument("extra", nargs=argparse.REMAINDER, help="after --: arguments passed to claude as they are")


def run_claude(args, exec_fn=None):
    extra = session.harness_extra(args, "tandem claude -- --model opus")
    meta = session.resolve_args(args)
    argv, env, cwd = prepare(meta, args.role, extra)
    (exec_fn or session.exec_harness)(argv, env, cwd)
    return 0
