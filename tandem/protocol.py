import os
import re

from tandem import TandemError, session, watch

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "protocol.md")
PEERS = {"claude": "Claude Code", "agy": "agy"}
PAIR_ROLES = ("driver", "navigator")
# agy caps each rules file at 24 KB; the brief is the only unbounded value, so it is cut well
# below that and the rest of the protocol always fits under 16 KB.
TASK_CAP = 6000

_MARKER = re.compile(r"^<!-- tandem: (shared|role [a-z]+|peer [a-z]+) -->$")
_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")


def load_template(path=TEMPLATE):
    """[(selector, text)] in file order; selector is "shared", "role <r>" or "peer <p>"."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as exc:
        raise TandemError("cannot read protocol template %s: %s" % (path, exc))
    sections = []
    for line in lines:
        m = _MARKER.match(line)
        if m:
            sections.append((m.group(1), []))
        elif sections:
            sections[-1][1].append(line)
        elif line.strip():
            raise TandemError("protocol template %s: text before the first section marker" % path)
    return [(sel, "\n".join(body).strip()) for sel, body in sections]


def peer_role(meta, peer):
    roles = meta.get("roles") or session.DEFAULT_ROLES
    for role in PAIR_ROLES:
        if roles.get(role) == peer:
            return role
    raise TandemError("%s holds no role in session %s" % (peer, meta.get("dir")))


def _other(role):
    return PAIR_ROLES[1 - PAIR_ROLES.index(role)]


def _brief(meta, sdir):
    ticket = (meta.get("ticket") or "").strip()
    task = (meta.get("task") or "").strip()
    parts = ["## Task"]
    if ticket:
        parts.append("Ticket: `%s`." % ticket)
    if task:
        if len(task) > TASK_CAP:
            task = "%s\n\n[Brief cut at %d characters; the full text is the `task` field of `%s`.]" % (
                task[:TASK_CAP].rstrip(),
                TASK_CAP,
                os.path.join(sdir, "session.json"),
            )
        parts.append("Brief from the user:\n\n" + task)
    if not ticket and not task:
        parts.append("No ticket or brief was given: the user's first prompt to the driver sets the task.")
    return "\n\n".join(parts)


def _tests(meta):
    cmds = [c for c in (meta.get("test_cmds") or []) if c and c.strip()]
    if not cmds:
        return "none configured; find the repo's own (README, Makefile) and tell your peer which you use."
    return ", ".join("`%s`" % c for c in cmds)


def values(meta, peer, role):
    sdir = session.session_path(meta["dir"])
    other = _other(role)
    roles = meta.get("roles") or session.DEFAULT_ROLES
    other_peer = roles.get(other) or session.DEFAULT_ROLES[other]
    return {
        "session": meta.get("name") or session.short_name(meta["dir"]),
        "session_dir": sdir,
        "role": role,
        "peer": PEERS[peer],
        "other_role": other,
        "other_peer": PEERS.get(other_peer, other_peer),
        "brief": _brief(meta, sdir),
        "tests": _tests(meta),
        "doorbell": watch.doorbell(role),
        "watch_cmd": "tandem watch --as %s --detach" % role,
    }


def _fill(text, vals):
    def sub(m):
        try:
            return vals[m.group(1)]
        except KeyError:
            raise TandemError("protocol template: unknown placeholder {{%s}}" % m.group(1))

    return _PLACEHOLDER.sub(sub, text)


def render(meta, peer, role=None, path=TEMPLATE):
    """The protocol for `peer` in `meta`'s session; `role` defaults to the one `peer` holds."""
    if peer not in PEERS:
        raise TandemError("unknown peer: %s" % peer)
    role = role or peer_role(meta, peer)
    if role not in PAIR_ROLES:
        raise TandemError("protocol is for driver or navigator, not %s" % role)
    wanted = ("shared", "role " + role, "peer " + peer)
    sections = load_template(path)
    for sel in wanted[1:]:
        if not any(s == sel for s, _ in sections):
            raise TandemError("protocol template has no %r section" % sel)
    vals = values(meta, peer, role)
    # One substitution pass, so placeholder-like text inside the user's brief stays literal.
    body = "\n\n".join(text for sel, text in sections if sel in wanted and text)
    return _fill(body, vals) + "\n"
