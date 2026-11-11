---
worth: later
where: tandem/cmdline.py (write_reason)
added: 2026-11-11
---
# agy navigator write gate misses indirect writes

The `agy-pretool` write gate reads the command line only. Writes it can't see: interpreters and
scripts (`python3 -c "open('f','w')"`, `node -e`, `./script.sh`), build tools (`make`, `npm install`,
`go generate`), `.git` metadata edited by hand, and wrapper options it doesn't know take a value
(the common ones of `sudo`, `nice`, `timeout`, `xargs`, `env` and `doas` are skipped with their
values). Heredoc bodies are parsed as commands, so a body line that starts with `rm` asks
needlessly (pinned in `tests/test_cmdline.py`). v0 accepts both: the gate is documented as best effort and
the driver's `git status` check at each checkpoint catches what it misses. Revisit if live sessions
show navigator writes slipping through or prompts on heredocs; a sandbox that keeps the workspace
read-only again would make the gate unnecessary.
