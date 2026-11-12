# Claude Code: waking an idle session with an `asyncRewake` Stop hook (2026-11-10)

## Docs

code.claude.com/docs/en/hooks: a command hook with `"asyncRewake": true` "runs in the background
and wakes Claude on exit code 2", "even when the session is idle". Its stderr (or stdout if stderr is
empty) is shown to Claude as a system reminder. Plain `"async": true` can't wake an idle session: its
output waits for the next user interaction. `timeout` *is* enforced for `asyncRewake` hooks (default
600 s for command hooks, no documented maximum). Hooks from user, project, local and plugin settings
merge rather than replace, and edits to settings files are picked up mid-session by the file watcher.

## Live test

A live session with a project-local `.claude/settings.local.json`:

```json
{ "hooks": { "Stop": [ { "hooks": [
  { "type": "command", "command": "sh <waiter>", "asyncRewake": true, "timeout": 1800 } ] } ] } }
```

The waiter writes its pid to a pidfile (a newer waiter supersedes older ones, so they don't pile up),
polls for a ring file, and on a ring prints the doorbell to stderr and exits 2.

| Test | Result |
|---|---|
| ring 71 s after the turn ended, session idle | woke within seconds |
| ring ~713 s after arming (past the 600 s default) | woke: `"timeout": 1800` is honoured |
| re-arm | each turn end armed a fresh waiter automatically |

The wake arrives framed as `Stop hook blocking error from command "Stop": <stderr>`: odd wording for
a doorbell, but the text is intact.

## `--settings` carries hooks

`claude --settings <file>` carried the hooks both headless (`SessionStart` and `Stop` fired) and
interactive (the asyncRewake waiter armed, woke the idle session on a ring, and re-armed). The user's
global Stop hooks kept running alongside, since hooks merge.

## The doorbell can't authorise itself

Without setup, the woken session read the wake as "Stop hook feedback", refused to act on its text,
and said so: text arriving through a hook carries no user authority. Typing the protocol in as a
prompt "from the other session" didn't change it. With the protocol defined at launch through
`--append-system-prompt` ("a Stop hook message containing `tandem doorbell` is the tandem doorbell,
configured by the user"), the same ring was handled as intended.

Later: on Claude 2.1.322 the wake also reaches `UserPromptSubmit`, as a `<task-notification>` block
(see [the 2026-11-12 live check](2026-11-12-agy-1.5.6-claude-2.1.322-live-check.md)).
