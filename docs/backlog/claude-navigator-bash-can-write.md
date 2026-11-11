---
worth: yes
where: tandem/launch_claude.py (build_settings)
added: 2026-11-11
---
# Claude as navigator can still write files through Bash

`tandem claude --role navigator` denies Edit, Write and NotebookEdit, but Bash stays allowed, so
`sed -i`, redirects or `touch` can still change the working tree. agy's navigator has a write gate
in its `PreToolUse` hook (agy 1.5.5's sandbox no longer makes the workspace read-only). v0 relies on
the driver's `git status` check at each checkpoint. The fix is a Claude navigator profile that closes
the shell channel (Claude Code's sandbox settings, or a `PreToolUse` hook on Bash that reuses
`tandem.cmdline.write_reason` and asks), deferred because Claude as navigator is the non-default role
and the safety net already catches violations.
