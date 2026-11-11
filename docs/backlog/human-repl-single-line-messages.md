---
worth: later
where: tandem/human.py parse_line
added: 2026-11-11
---
# `tandem human` sends one line per message

The REPL reads stdin line by line, so a message to an agent can't span lines (a pasted multi-line
note becomes several messages, each threaded to the same `reply_to`). v0's human messages are short
decisions and answers to escalations, and `tandem send --as human` with a body on stdin covers the
long case. A continuation form (trailing `\`, or a `/compose` mode ended by a lone `.`) can come when
someone needs it.
