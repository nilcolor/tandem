---
worth: later
where: tandem/watch.py (detach, _stamp)
added: 2026-11-11
---
# agy watcher log grows without bound

`tandem watch --detach` appends to `peers/<role>/watch.log` across every restart, and a failing
`agentapi` call adds a line every retry (5 s). A session is bounded by its task and the file lives in
the session dir, so v0 lives with it; appending keeps the previous watcher's exit reason, which the
watchdog case needs when debugging. If long sessions with a broken agentapi show up, rotate at start
(keep one `.1`) or log a repeated failure only when its text changes.
