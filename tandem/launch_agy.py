import os

from tandem import TandemError, paths, protocol, session

PEER = "agy"
# agy's hooks.json top-level keys are hook names; event names there load nothing, silently.
HOOK_NAME = "tandem"
HOOK_EVENTS = (
    ("PreToolUse", "agy-pretool"),
    ("SessionStart", "agy-session-start"),
    ("PreInvocation", "agy-preinvocation"),
    ("Stop", "agy-stop"),
)
# Only run_command needs a decision; every other tool keeps agy's own policy.
PRETOOL_MATCHER = "run_command"
RULES_CAP = 24 * 1024
PROFILES = ("navigator", "fallback", "driver")
READ_ONLY_GIT = ("git status", "git diff", "git log", "git show")
DENY_FILE_TOOL = ["write_file(*)"]


def peer_dir(sdir):
    return os.path.join(sdir, "peers", PEER)


def profile_path(sdir):
    return os.path.join(peer_dir(sdir), "profile.json")


def agents_dir(sdir):
    return os.path.join(sdir, ".agents")


def hooks_path(sdir):
    return os.path.join(agents_dir(sdir), "hooks.json")


def rules_path(sdir):
    return os.path.join(agents_dir(sdir), "rules", "AGENTS.md")


def build_hooks(meta):
    def handler(hook):
        return {"type": "command", "command": paths.shim_command("--session", meta["dir"], "hook", hook)}

    events = {}
    for event, hook in HOOK_EVENTS:
        if event == "PreToolUse":
            events[event] = [{"matcher": PRETOOL_MATCHER, "hooks": [handler(hook)]}]
        else:
            events[event] = [handler(hook)]
    return {HOOK_NAME: events}


def rule_prefixes(meta):
    """session.json's test commands as agy rule prefixes, whitespace collapsed, in order, once each."""
    out = []
    for cmd in meta.get("test_cmds") or []:
        if not isinstance(cmd, str):
            continue
        text = " ".join(cmd.split())
        # A parenthesis would end the rule early and agy would read a different prefix.
        if not text or "(" in text or ")" in text or text in out:
            continue
        out.append(text)
    return out


def _rules(kind, prefixes):
    seen = []
    for prefix in prefixes:
        rule = "%s(%s)" % (kind, prefix)
        if rule not in seen:
            seen.append(rule)
    return seen


def profile_kind(role, fallback=False):
    if role == "driver":
        return "driver"
    if role == "navigator":
        return "fallback" if fallback else "navigator"
    raise TandemError("agy profiles are for driver or navigator, not %s" % role)


def build_profile(meta, kind):
    """The `--settings` profile for `kind`.

    `--settings` replaces `permissions.allow` and `permissions.deny` separately, so both are
    always written: an omitted key would keep the user's global list.
    """
    tests = rule_prefixes(meta)
    if kind == "navigator":
        return {
            "toolPermission": "proceed-in-sandbox",
            "enableTerminalSandbox": True,
            "permissions": {"allow": _rules("unsandboxed", tests + ["tandem"]), "deny": list(DENY_FILE_TOOL)},
        }
    if kind == "fallback":
        return {
            "toolPermission": "request-review",
            "enableTerminalSandbox": False,
            "permissions": {
                "allow": _rules("command", ["tandem"] + list(READ_ONLY_GIT) + tests),
                "deny": list(DENY_FILE_TOOL),
            },
        }
    if kind == "driver":
        return {
            "toolPermission": "request-review",
            "enableTerminalSandbox": False,
            "permissions": {"allow": _rules("command", ["tandem", "git"]), "deny": []},
        }
    raise TandemError("unknown agy profile %r (one of %s)" % (kind, ", ".join(PROFILES)))


def write_profile(meta, kind, path=None):
    """Write the `kind` profile (default path: peers/agy/profile.json); returns the path."""
    path = path or profile_path(session.session_path(meta["dir"]))
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
