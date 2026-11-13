import argparse
import json
import os
import subprocess
import sys

from tandem import TandemError, hooks, paths, protocol, session, watch

PEER = "agy"
RULES_CAP = 24 * 1024
READ_ONLY_GIT = ["git status", "git diff", "git log", "git show"]
DENY_FILE_TOOL = ["write_file(*)"]
SESSION_FILES = ("plan.md", "decisions.md")
# `--mode auto` needs the terminal sandbox, which the driver profile turns off, and agy then
# falls back to the default mode silently ("auto mode is unavailable" in agy 1.5.6's log;
# docs/probes 2026-11-12 live check).
DRIVER_MODE = "accept-edits"
# The user's own agy settings, read for their deny rules and never written.
USER_SETTINGS = os.path.join("~", ".gemini", "antigravity-cli", "settings.json")
# The first agy the profiles, hooks and watcher were checked live on (docs/probes 2026-11-12 live
# check). 1.5.4 and 1.5.5 behave differently where it matters (sandbox, bypass rules), and the
# profiles assume neither.
MIN_AGY_VERSION = (1, 5, 6)
VERSION_TIMEOUT = 30


def profile_path(sdir):
    return os.path.join(session.peer_dir(sdir, PEER), "profile.json")


def agents_dir(sdir):
    return os.path.join(sdir, ".agents")


def hooks_path(sdir):
    return os.path.join(agents_dir(sdir), "hooks.json")


def rules_path(sdir):
    return os.path.join(agents_dir(sdir), "rules", "AGENTS.md")


def build_hooks(meta):
    def entry(hook):
        handler = {"type": "command", "command": paths.shim_command("--session", meta["dir"], "hook", hook.name)}
        # Only run_command needs a decision, so every other tool keeps agy's own policy.
        if hook.event == "PreToolUse":
            return [{"matcher": "run_command", "hooks": [handler]}]
        return [handler]

    # Top-level keys are hook names; event names there load nothing, silently.
    return {"tandem": {h.event: entry(h) for h in hooks.HOOKS.values() if h.peer == PEER}}


def user_deny():
    """The deny rules in the user's global agy settings; [] when the file is missing or unreadable."""
    path = os.path.expanduser(USER_SETTINGS)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return []
    perms = doc.get("permissions") if isinstance(doc, dict) else None
    deny = perms.get("deny") if isinstance(perms, dict) else None
    if not isinstance(deny, list):
        return []
    return [rule for rule in deny if isinstance(rule, str) and rule.strip()]


def read_only_git(repo):
    """The navigator's read-only git prefixes, plus their `git -C <repo>` forms.

    agy reaches for `git -C <absolute repo> diff` on its own, and a rule matches the command text,
    so without these the navigator asks before every such read (docs/probes 2026-11-12 live
    check). A path holding `(` or `)` would end the rule early, so it gets only the plain forms.
    """
    forms = list(READ_ONLY_GIT)
    if "(" not in repo and ")" not in repo:
        forms += ["git -C %s%s" % (repo, cmd[len("git"):]) for cmd in READ_ONLY_GIT]
    return forms


def session_file_writes(meta):
    """`write_file(...)` rules for the two session files the driver owns.

    They sit outside the workspace, where `accept-edits` asks before each write; agy 1.5.6 honours
    allowlisted `write_file` rules instead (docs/probes 2026-11-12 live check). A path holding `(`
    or `)` would end the rule early, so it gets none and the driver is asked.
    """
    sdir = session.session_path(meta["dir"])
    if "(" in sdir or ")" in sdir:
        return []
    return ["write_file(%s)" % os.path.join(sdir, name) for name in SESSION_FILES]


def build_profile(meta, role):
    """The `--settings` profile for agy as `role`.

    Both keep the sandbox off and terminal commands under review, whatever the user's global
    settings say, so the allowlist decides what runs unasked. `--settings` replaces
    `permissions.allow` and `permissions.deny` separately, so both are always written: an omitted
    key would keep the user's global list. A written `deny` replaces the user's global deny too,
    so theirs is carried over after tandem's own.
    """
    prefixes = protocol.approved_prefixes(meta, role)
    if role == "driver":
        allow = ["command(%s)" % p for p in dict.fromkeys(prefixes + ["git"])] + session_file_writes(meta)
        deny = []
    else:
        allow = ["command(%s)" % p for p in dict.fromkeys(prefixes + read_only_git(meta["repo"]))]
        deny = list(DENY_FILE_TOOL)
    return {
        "toolPermission": "request-review",
        "enableTerminalSandbox": False,
        "permissions": {"allow": allow, "deny": list(dict.fromkeys(deny + user_deny()))},
    }


def write_profile(meta, role):
    """Write the profile for `role` to peers/agy/profile.json; returns the path."""
    path = profile_path(session.session_path(meta["dir"]))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    session.write_json_atomic(path, build_profile(meta, role))
    return path


def render_rules(meta, role):
    text = protocol.render(meta, PEER, role)
    size = len(text.encode("utf-8"))
    if size > RULES_CAP:
        raise TandemError("agy rules for session %s are %d bytes, over agy's %d-byte cap" % (meta["dir"], size, RULES_CAP))
    return text


def write_agents_dir(meta, role):
    """Write `.agents/hooks.json` and `.agents/rules/AGENTS.md`, which agy loads via `--add-dir`."""
    sdir = session.session_path(meta["dir"])
    text = render_rules(meta, role)
    os.makedirs(os.path.dirname(rules_path(sdir)), exist_ok=True)
    session.write_json_atomic(hooks_path(sdir), build_hooks(meta))
    session.write_text_atomic(rules_path(sdir), text)
    return agents_dir(sdir)


def write_session_files(meta, role):
    """Everything agy reads from the session dir for `role`."""
    write_agents_dir(meta, role)
    write_profile(meta, role)
    protocol.write_guides(session.session_path(meta["dir"]))


def agy_version():
    """`agy --version`'s output, or None when agy didn't answer in time or failed."""
    try:
        proc = subprocess.run(
            ["agy", "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            errors="replace",
            timeout=VERSION_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError as exc:
        raise TandemError("cannot run agy: %s" % exc)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def version_number(version):
    """`agy --version`'s last word as a tuple of ints, or None when it isn't one."""
    words = (version or "").split()
    try:
        return tuple(int(part) for part in words[-1].split("."))
    except (IndexError, ValueError):
        return None


def require_agy_version():
    """`agy --version`'s output; TandemError when it names an agy older than MIN_AGY_VERSION.

    A version tandem can't read only warns: a changed output format in a later agy shouldn't lock
    every user out.
    """
    wanted = ".".join(str(n) for n in MIN_AGY_VERSION)
    version = agy_version()
    number = version_number(version)
    if number is None:
        seen = "no answer from `agy --version`" if version is None else "`agy --version` printed %r" % version
        print("tandem: can't tell agy's version (%s); tandem needs agy %s or later" % (seen, wanted), file=sys.stderr)
    elif number < MIN_AGY_VERSION:
        raise TandemError("agy %s is too old: tandem needs agy %s or later (run `agy update`)" % (version, wanted))
    return version


def initial_prompt(meta, role):
    return protocol.initial_prompt(
        meta,
        role,
        "If tandem's session message says your watcher is not running, start it first by running exactly `%s`."
        % watch.detach_command(role),
    )


def prepare(meta, role=None, extra=()):
    """Check agy, claim the role, write the session's agy files; returns (argv, env, cwd) to exec."""
    version = require_agy_version()
    meta = session.claim_role(meta["dir"], PEER, role)
    role = protocol.peer_role(meta, PEER)
    sdir = session.session_path(meta["dir"])
    write_session_files(meta, role)
    session.log_event(sdir, "agy_profile", profile=role, version=version)

    argv = ["agy", "--settings", profile_path(sdir), "--add-dir", sdir]
    if role == "driver":
        argv += ["--mode", DRIVER_MODE]
    argv.append("--prompt-interactive=%s" % initial_prompt(meta, role))
    argv += list(extra)
    return argv, session.harness_env(meta, role), meta["repo"]


def configure_agy(p):
    p.add_argument("--role", choices=session.PAIR_ROLES, help="role to claim (default: the one session.json gives agy)")
    p.add_argument("extra", nargs=argparse.REMAINDER, help="after --: arguments passed to agy as they are")


def run_agy(args, exec_fn=None):
    extra = session.harness_extra(args, "tandem agy -- --model <name>")
    meta = session.resolve_args(args)
    argv, env, cwd = prepare(meta, args.role, extra)
    (exec_fn or session.exec_harness)(argv, env, cwd)
    return 0
