---
worth: later
where: tandem/templates/protocol.md (role navigator)
added: 2026-11-13
---
# A Claude navigator could run the final review's lenses as parallel subagents

The final review's comprehensive phase has five lenses, and a navigator goes through them one
after another. A Claude navigator could fan them out to subagents and merge the findings, which is
faster and gives each lens a fresh context. Deferred because it hasn't been checked that a
subagent keeps the navigator's `Edit`/`Write`/`NotebookEdit` deny from `--settings`; a subagent
that can edit would break the navigator's read-only rule. agy has no equivalent, so the protocol
would gain a Claude-only line in the peer section.
