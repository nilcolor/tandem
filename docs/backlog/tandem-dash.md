---
worth: yes
added: 2026-11-11
---
# tandem dash: web dashboard over a session

The user wants a web view of a running session: `log.jsonl`, `plan.md`, `decisions.md` and the
inboxes, live. Not designed yet and out of v0 scope. Feasibility is already recorded in
`docs/design.md` (`Dashboard` section): Python stdlib `ThreadingHTTPServer` plus SSE, with the `log.jsonl` byte offset
as the event id so `EventSource` reconnects resume, and POST for sending as human.
