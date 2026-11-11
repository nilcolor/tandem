---
worth: later
where: tandem/session.py claim_role
added: 2026-11-11
---
# A second launcher for the same peer isn't refused

`tandem claude` run twice in one session (two panes) overwrites the first claim and the shared
`peers/claude/settings.json`; both Claude sessions then arm Stop-hook waiters for the same role, and
each new waiter supersedes the other's, so only the one that ended its turn last gets the doorbell.
v0 only refuses taking a role the *other* peer holds live, which is what the plan asks for. Refusing a
live claim by the same peer would also block a relaunch whenever a pid is reused, so it was deferred
until someone actually hits the double launch.
