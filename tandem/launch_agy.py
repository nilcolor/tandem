import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

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
# Versions where a live session confirmed `unsandboxed(...)` bypasses selectively (docs/probes).
VERIFIED_AGY_VERSIONS = frozenset(["1.5.4", "1.5.5"])
# 1.5.6 dropped `unsandboxed(...)` rules and asks before every sandbox bypass, so the sandboxed
# profile would prompt on each tandem command; `/config` still echoes the rules, so the
# self-check can't see it (docs/probes 2026-11-12 live check).
UNSANDBOXED_REMOVED_IN = (1, 5, 6)
CHECK_TIMEOUT = 30
# The 1.5.5 TUI notice for rules agy refuses (docs/probes 2026-11-11 agy 1.5.5 re-check).
UNSUPPORTED_MARKERS = ('Invalid "unsandboxed" permission rules', "unsandboxed is no longer a valid rule")
PROFILE_CHOICES = ("auto", "fallback")


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


def profile_kind(role, fallback=False):
    if role == "driver":
        return "driver"
    return "fallback" if fallback else "navigator"


def read_only_git(repo):
    """The navigator's read-only git prefixes, plus their `git -C <repo>` forms.

    agy reaches for `git -C <absolute repo> diff` on its own, and a rule matches the command text,
    so without these the fallback navigator asks before every such read (docs/probes 2026-11-12
    live check). A path holding `(` or `)` would end the rule early, so it gets only the plain forms.
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


def build_profile(meta, kind):
    """The `--settings` profile for `kind`: "navigator", "fallback" or "driver".

    `--settings` replaces `permissions.allow` and `permissions.deny` separately, so both are
    always written: an omitted key would keep the user's global list. A written `deny`
    replaces the user's global deny too, so theirs is carried over after tandem's own.
    """
    role = "driver" if kind == "driver" else "navigator"
    prefixes = protocol.approved_prefixes(meta, role)
    if kind == "navigator":
        settings = {"toolPermission": "proceed-in-sandbox", "enableTerminalSandbox": True}
        allow, deny = ["unsandboxed(%s)" % p for p in prefixes], list(DENY_FILE_TOOL)
    else:
        settings = {"toolPermission": "request-review", "enableTerminalSandbox": False}
        extra = ["git"] if kind == "driver" else read_only_git(meta["repo"])
        allow = ["command(%s)" % p for p in dict.fromkeys(prefixes + extra)]
        if kind == "driver":
            allow += session_file_writes(meta)
        deny = [] if kind == "driver" else list(DENY_FILE_TOOL)
    settings["permissions"] = {"allow": allow, "deny": list(dict.fromkeys(deny + user_deny()))}
    return settings


def write_profile(meta, kind):
    """Write the `kind` profile to peers/agy/profile.json; returns the path."""
    path = profile_path(session.session_path(meta["dir"]))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    session.write_json_atomic(path, build_profile(meta, kind))
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


def write_session_files(meta, role, fallback=False):
    """Everything agy reads from the session dir for `role`; returns the profile kind written."""
    kind = profile_kind(role, fallback)
    write_agents_dir(meta, role)
    write_profile(meta, kind)
    return kind


def check_path():
    return os.path.join(paths.tandem_home(), "agy-check.json")


def _run(argv, cwd=None):
    """Run a print-mode agy command; returns (returncode, stdout, stderr), or None on timeout."""
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            errors="replace",
            cwd=cwd,
            timeout=CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError as exc:
        raise TandemError("cannot run %s: %s" % (argv[0], exc))
    return proc.returncode, proc.stdout, proc.stderr


def agy_version():
    """`agy --version`'s output, or None when agy didn't answer."""
    result = _run(["agy", "--version"])
    if result is None or result[0] != 0 or not result[1].strip():
        return None
    return result[1].strip()


def is_verified(version):
    words = (version or "").split()
    return bool(words) and words[-1] in VERIFIED_AGY_VERSIONS


def version_number(version):
    """`agy --version`'s last word as a tuple of ints, or None when it isn't one."""
    words = (version or "").split()
    try:
        return tuple(int(part) for part in words[-1].split("."))
    except (IndexError, ValueError):
        return None


def has_unsandboxed_rules(version):
    number = version_number(version)
    return number is None or number < UNSANDBOXED_REMOVED_IN


def parse_check(profile, result):
    """(why `profile` didn't load as written or None, whether that answer holds for this version).

    Only a config agy printed, or its notice that the rules are unsupported, is an answer about
    the version. A timeout, a failed run or unreadable output can come from an expired login, an
    update banner or a first-run prompt, so those are re-checked on the next launch.
    """
    if result is None:
        return "agy -p=/config timed out after %ds" % CHECK_TIMEOUT, False
    code, out, err = result
    for marker in UNSUPPORTED_MARKERS:
        if marker in err or marker in out:
            return "agy says unsandboxed rules are unsupported", True
    if code != 0:
        first = (err.strip() or out.strip() or "no output").splitlines()[0]
        return "agy -p=/config exited %d: %s" % (code, first), False
    try:
        config = json.loads(out)["command"]["data"]["config"]
    except (ValueError, KeyError, TypeError):
        config = None
    if not isinstance(config, dict):
        return "agy -p=/config printed no config", False
    for key in ("toolPermission", "enableTerminalSandbox"):
        if config.get(key) != profile[key]:
            return "%s is %r, not %r" % (key, config.get(key), profile[key]), True
    perms = config.get("permissions")
    for key in ("allow", "deny"):
        shown = perms.get(key) if isinstance(perms, dict) else None
        missing = [r for r in profile["permissions"][key] if not isinstance(shown, list) or r not in shown]
        if missing:
            return "permissions.%s lost %s" % (key, ", ".join(missing)), True
    return None, True


def read_check(version):
    try:
        with open(check_path(), encoding="utf-8") as f:
            cached = json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(cached, dict) and cached.get("version") == version and isinstance(cached.get("ok"), bool):
        return cached
    return None


def self_check(meta, version):
    """Whether this agy (`version`, from agy_version) loads the navigator profile as written.

    Returns the check record. `/config` echoes rule strings without validating them, so this
    proves only that the profile parses and its rules survive loading. Whether
    `unsandboxed(...)` still bypasses is known only for VERIFIED_AGY_VERSIONS. A definitive
    result is cached per `agy --version` (the record then has `cached: true`).
    """
    if version is None:
        return {"version": None, "ok": False, "reason": "agy --version gave no answer"}
    cached = read_check(version)
    if cached is not None:
        return dict(cached, cached=True)
    profile = build_profile(meta, "navigator")
    tmp = tempfile.mkdtemp(prefix="tandem-agy-check-")
    try:
        path = os.path.join(tmp, "profile.json")
        session.write_json_atomic(path, profile)
        result = _run(["agy", "--settings", path, "-p=/config", "--output-format", "json"], cwd=tmp)
    finally:
        shutil.rmtree(tmp, True)
    reason, definitive = parse_check(profile, result)
    record = {"version": version, "ok": reason is None, "reason": reason or "", "ts": session.now_iso()}
    if definitive:
        paths.make_private_dir(paths.tandem_home())
        session.write_json_atomic(check_path(), record)
    return record


def choose_fallback(meta, profile):
    """(use the fallback profile?, check record or None) for a navigator; prints why when unsure."""
    if profile == "fallback":
        return True, None
    version = agy_version()
    if not has_unsandboxed_rules(version):
        print(
            "tandem: agy %s has no `unsandboxed(...)` rules, so the sandboxed profile would ask before every "
            "tandem command; using the fallback profile (no sandbox, tight allowlist)" % version,
            file=sys.stderr,
        )
        return True, {"version": version, "ok": False, "reason": "no unsandboxed rules in this agy"}
    check = self_check(meta, version)
    if not check["ok"]:
        how = " (cached; delete %s to re-run the check)" % check_path() if check.get("cached") else ""
        print(
            "tandem: agy self-check failed (%s)%s; using the fallback profile (no sandbox, tight allowlist)"
            % (check["reason"], how),
            file=sys.stderr,
        )
        return True, check
    if not is_verified(check["version"]):
        print(
            "tandem: the sandbox bypass is unverified on agy %s (verified: %s); if tests or tandem commands "
            "prompt or fail, relaunch with `tandem agy --profile fallback`"
            % (check["version"], ", ".join(sorted(VERIFIED_AGY_VERSIONS))),
            file=sys.stderr,
        )
    return False, check


def initial_prompt(meta, role):
    return protocol.initial_prompt(
        meta,
        role,
        "If tandem's session message says your watcher is not running, start it first by running exactly `%s`."
        % watch.detach_command(role),
    )


def prepare(meta, role=None, profile="auto", extra=()):
    """Claim the role, check agy, write the session's agy files; returns (argv, env, cwd) to exec."""
    meta = session.claim_role(meta["dir"], PEER, role)
    role = protocol.peer_role(meta, PEER)
    sdir = session.session_path(meta["dir"])
    fallback, check = False, None
    if role == "navigator":
        fallback, check = choose_fallback(meta, profile)
    elif profile == "fallback":
        print("tandem: --profile fallback applies to the navigator only; the driver profile is used", file=sys.stderr)
    kind = write_session_files(meta, role, fallback)
    fields = {"profile": kind}
    if check is not None:
        fields.update(version=check["version"], check="ok" if check["ok"] else check["reason"])
    session.log_event(sdir, "agy_profile", **fields)

    argv = ["agy", "--settings", profile_path(sdir), "--add-dir", sdir]
    # Driver mode doesn't show in `/config`; only a live session confirms it took.
    if kind == "driver":
        argv += ["--mode", DRIVER_MODE]
    argv.append("--prompt-interactive=%s" % initial_prompt(meta, role))
    argv += list(extra)
    return argv, session.harness_env(meta, role), meta["repo"]


def configure_agy(p):
    p.add_argument("--role", choices=session.PAIR_ROLES, help="role to claim (default: the one session.json gives agy)")
    p.add_argument(
        "--profile",
        choices=PROFILE_CHOICES,
        default="auto",
        help="navigator profile: auto runs the self-check and uses the sandbox profile if it passes "
        "(agy 1.5.6 and later always get fallback); fallback skips the sandbox (default: auto)",
    )
    p.add_argument("extra", nargs=argparse.REMAINDER, help="after --: arguments passed to agy as they are")


def run_agy(args, exec_fn=None):
    extra = session.harness_extra(args, "tandem agy -- --model <name>")
    meta = session.resolve_args(args)
    argv, env, cwd = prepare(meta, args.role, args.profile, extra)
    (exec_fn or session.exec_harness)(argv, env, cwd)
    return 0
