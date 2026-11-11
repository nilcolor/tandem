# tandem

A pairing protocol for two coding agents working one task together. Claude Code and Antigravity CLI
(`agy`) come first; Copilot or Codex can be swapped in later. Typical flow: the user hands over a task
(a Jira ticket, a brief, or just a first prompt), the driver writes a plan, the navigator challenges
it, they converge or escalate to the human, the driver implements in checkpoints while the navigator
reviews each one, and they finish with a summary of what was done and which decisions were made and
why.

Status: design done, v0 implementation next (Python 3.9+, stdlib only). No code yet. This file is the
decision record.

## Hard requirements (from the user)

- **Harness, peer and terminal agnostic.** No dependency on agterm's mailbox and no pasting message
  text into a harness prompt as the transport.
- **Messages go through a shared per-session store** (an inbox or bus), as text data.
- **Both agents run interactively, in their own visible TUI.** The user watches the conversation and
  can type to either agent at any time. A headless agent is out.
- **Claude usually drives.** Either agent can drive, but the default is Claude as driver, agy as
  navigator.

## Roles

- **Driver**: the only agent allowed to edit. Owns `plan.md` and `decisions.md`.
- **Navigator**: reads, reviews, challenges, helps decide. Never edits.
- Vocabulary is pair programming, so prompts need no definitions.
- The single-writer rule is enforced at the harness permission level, not by prompt text, because a
  reviewer that spots a bug will "just fix it" despite instructions. Claude as navigator: deny rules
  for Edit and Write. agy as navigator: the terminal sandbox (workspace read-only) plus a
  `write_file(*)` deny, both passed through `--settings`; see
  [Permissions](#permissions-autonomy-without-prompt-fatigue).
- The navigator reviews evidence (diffs, commits, test runs it ran itself), not chat claims.

## Transport: maildir per session, no daemon

Sessions live in a **global spool**, `~/.tandem/sessions/<session>/`, not in the target repo. That
keeps the bus out of the repo's git state, and `--add-dir` attaches it to agy's workspace anyway.

### Naming

The session name is only a label for addressing; the ticket is separate, optional input for intake.

| Command | Name |
|---|---|
| `tandem init` | derived from the branch (`feature/login-timeout` → `login-timeout`); on detached HEAD or `main`, `<repo>-<MMDD-HHMM>` |
| `tandem init fix-csv-bom` | any free-form name |
| `tandem init --ticket APIX-123` | defaults to the ticket key; the ticket is stored for intake |

- Names are sanitized to `[a-z0-9-]` on disk; the original string is kept in `session.json`.
- The spool is global, so the directory is prefixed with the repo, `~/.tandem/sessions/apix--fix-csv-bom/`,
  while the user keeps typing just `fix-csv-bom` (resolution is per repo). A collision in the same
  repo appends `-2`.
- **Task source for intake**, whichever exists: the linked ticket, `--task "…"` / `--task-file brief.md`
  given to `init`, or the user's first prompt in the driver's console (captured by the human-input
  hook).

### Finding the session from any pane

An env var doesn't cross panes or windows, so the session is keyed to **the repo**. `TANDEM_SESSION`
is only how a launcher hands the session to the harness it starts (and from there to its hooks and to
`tandem send/recv`).

- `tandem init` in a repo creates the session, records the repo path in `session.json`, and points
  the repo's active-session pointer at it: `~/.tandem/repos/<hash of git toplevel>/current → <session>`.
- `tandem claude` / `tandem agy` run in **any** pane or window inside that repo resolve the pointer,
  export `TANDEM_SESSION` and `TANDEM_ROLE` into the harness, and `exec` it.

Resolution order, first match wins:

| # | Source | Used by |
|---|---|---|
| 1 | `--session <name>` | explicit override, from anywhere |
| 2 | `TANDEM_SESSION` | anything inside a launched harness: hooks, `tandem send/recv` |
| 3 | the active session for the current repo (`git rev-parse --show-toplevel`, so subdirectories work) | launchers, the normal case |
| 4 | the only active session on the machine | `tandem human` outside any repo |
| 5 | otherwise: error listing the active sessions | ambiguity is never guessed |

- **Worktrees** have different toplevels, so parallel tasks in worktrees get separate sessions for free.
- Step 4 applies only outside a git repo. Inside a repo (or a worktree) without its own active session,
  taking the machine's only session would attach a launcher to another tree's plan, so it is an error.
- Steps 1 and 2 return an ended session too; each command decides whether that is an error (hooks and
  watchers exit quietly on one). A `--session` short name matches the current repo's sessions first,
  then all sessions; among several matches, the single active one wins.
- **One active session per checkout.** `tandem init` refuses while one is active unless `--replace`:
  one working tree can't take two drivers anyway.
- **Ending.** `tandem done` writes the summary and clears the repo pointer. A stale pointer (a session
  never closed) shows in `tandem status`; `init --replace` takes over from it.
- `init` stays explicit in v0; launchers don't auto-init.

```
~/.tandem/sessions/<session>/
  session.json       # name as typed, repo path, ticket/task, roles, created/ended
  peers/claude/      # settings generated by `tandem claude` (hooks, permissions, protocol prompt)
  .agents/           # agy hooks, rules, plugin; loaded via `agy --add-dir <session-dir>`
  inbox/driver/{tmp,new,cur}/
  inbox/navigator/{tmp,new,cur}/
  inbox/human/{tmp,new,cur}/
  log.jsonl          # append-only copy of every message; input for the final summary
  plan.md            # driver-owned
  decisions.md       # driver-owned; the summary derives from it
  review-N.md        # optional per-checkpoint review artifacts
```

- Delivery: write to `tmp/`, then `mv` to `new/`. The reader never sees a partial message.
- Ack: move from `new/` to `cur/`. Crash recovery is free.

### The human

The user takes part two ways, and both are first-class:

- **Typing into either agent's console** (usually the driver's). This is the natural one, but it
  bypasses the bus, so each launcher installs a hook that appends every prompt the user types to
  `log.jsonl` as `from: human`: `UserPromptSubmit` for Claude, `PreInvocation`'s `lastUserInput` for
  agy. Decisions made in a console then still reach the summary.
- **`tandem human`** from any terminal: reads the `human` inbox (escalations, questions) and sends to
  either role.

No separate notifier. An agent that escalates also says so in its own console, so the user sees it
where they are already looking; the skill requires it.
- Rejected: a unix socket broker (a daemon to manage, with nothing gained over a directory); the
  agterm mailbox (not terminal agnostic); pasting content into prompts (the `peer-chat` approach).

### Envelope

```json
{"id":"0007","from":"driver","to":"navigator","type":"review_request",
 "reply_to":"0005","expects_reply":true,
 "refs":{"commit":"abc123","files":["plan.md"]},
 "body":"markdown..."}
```

Types: `plan`, `review_request`, `review`, `question`, `answer`, `decision`, `escalate`, `done`.
`expects_reply` plus a `recv --timeout` that escalates to `human` prevents mutual-wait deadlock.

- Ids are per session, shared by all three inboxes, gap-free: the next id is one above the highest
  message in any `new/` or `cur/`, allocated under the session's `flock`, so `reply_to` names one
  message whichever inbox it sits in. `--reply-to` must name an existing message.
- `--type` defaults to `answer` with `--reply-to`, else `question`. A reply that defaulted to
  `question` would expect a reply back, and two agents would answer each other's answers forever.
- `--commit` is resolved in the session's repo and stored as a 12-char sha, so `HEAD` keeps meaning
  the commit it named when the message was sent. `--file` paths are stored as given.

### Launchers: `tandem <peer>` is the per-peer adapter

Each peer needs a specific launch: a permission profile, hooks, the doorbell protocol in its
instructions, session env, and a startup self-check. The launcher sets all of that up and then `exec`s
the real binary, so the harness owns the terminal directly (no wrapper process, no PTY). tandem never
opens panes: the user runs each launcher in whatever terminal they like.

| Command | Does |
|---|---|
| `tandem claude [--role driver\|navigator]` | writes per-session Claude settings (Stop hook with `asyncRewake`, `UserPromptSubmit` human-input log, permissions) into the session dir; `exec claude --settings <file> --append-system-prompt <protocol>` |
| `tandem agy [--role navigator\|driver]` | writes the agy profile (sandbox + `unsandboxed(<test cmd>)` for the navigator, `--mode auto` for a driver) and the hooks/rules into the session dir; `exec agy --settings <file> --add-dir <session-dir>` |
| `tandem human` | small REPL: tails the `human` inbox (escalations, questions) and sends to either role |
| `tandem dash` (later) | web dashboard over the session dir (`log.jsonl`, `plan.md`, `decisions.md`, inboxes); not designed yet, feasibility noted in [Dashboard](#dashboard-tandem-dash-later-stdlib-is-enough) |

- Defaults follow the user's preference: Claude drives, agy navigates. `--role` flips them, and the
  launcher picks the matching profile.
- Launchers export `TANDEM_SESSION` and `TANDEM_ROLE`, so hooks and `tandem send/recv` know who they
  are without flags.
- **Protocol parity.** Both peers get the doorbell protocol and role rules from launch-time,
  user-authored instructions: Claude through `--append-system-prompt` (or the tandem skill), agy
  through rules loaded with the session (plugin `rules/` or `.agents/` via `--add-dir`). agy followed
  doorbell text in the probes without setup, but parity means we don't rely on that.
- **agy customizations ship in the session dir via `--add-dir` (verified).** From an added dir, agy
  loads `.agents/hooks.json`, `.agents/rules/AGENTS.md`, a root `AGENTS.md`, `.agents/skills/<name>/`,
  and plugins under `.agents/plugins/<name>/` with their own hooks, rules and skills (plugin skills
  are namespaced `<plugin>:<skill>`). Checked with print-mode `/hooks`, `/skills` and `/plugin list`
  (no model turn), and for rules with marker words agy reported from context, with no tool calls in
  the event stream. Nothing is installed globally and nothing is written into the target repo.
- Consequence: the added session dir is part of agy's **workspace**, which the sandbox makes
  read-only. The navigator's `tandem send/recv` writes there, so it needs the same treatment as tests:
  the hook forces `BypassSandbox` for `tandem …` commands, and the profile carries `unsandboxed(tandem)`.
- New peers (Copilot, Codex) are added as new launchers; the protocol, bus and skill don't change.

### Implementation: Python 3.9+, stdlib only

The team runs macOS and Linux, so tandem is plain Python source that runs unchanged on both, with no
build step and no dependencies.

- **Layout: a `tandem/` package plus a `bin/tandem` shim** (`#!/usr/bin/env python3`, no `.py`
  extension) that resolves its own realpath and imports the package. Install is a clone plus a
  symlink of `bin/tandem` into `~/.local/bin` (`make install`). Hooks and agy's
  `unsandboxed(tandem)` rule see a plain command, and every hook is `tandem hook <event>`, so hook
  logic is tested Python rather than generated shell.
- **3.9 floor**, because `/usr/bin/python3` from Apple's Command Line Tools is 3.9.6 and Linux distros
  ship 3.9–3.12. `env python3` picks whichever comes first on PATH, so nothing newer may be assumed.
- **Stdlib only** (`json`, `os.rename` for maildir delivery, `hashlib`, `argparse`, `subprocess`,
  `unittest`): no venv, no pip, no dependency on a teammate's machine.
- **No `zipapp`** (user's call). If distribution ever needs a single-file artifact instead of a clone,
  that's the point to revisit the language and move to a compiled binary (Go, Rust, or Swift),
  not to bundle Python. Swift caveat: Linux support exists but is a second-class target compared to
  macOS, which matters for a mixed team.
- Startup is ~40 ms per call, fine even for agy's `PreToolUse` hook on every `run_command`.
- Rejected:
  - **Shell + `jq`** (the earlier plan): the watcher (pidfile supersede, following the
    conversation ID, exiting on a stale LS address) and session resolution (hashing, sanitizing,
    collisions) are state logic that gets fragile in shell, and there's no test tooling for it.
  - **Go**: cross-compiling is cheap, but a release pipeline for four targets (darwin and linux on
    arm64 and amd64), or Go on every teammate's machine, buys nothing v0 needs. Reconsider if a
    single static artifact with no runtime assumptions becomes important.

### Dashboard (`tandem dash`, later): stdlib is enough

Not designed yet; this only records that Python stdlib won't block it.

- **Push via SSE, not WebSockets.** `http.server.ThreadingHTTPServer` (one thread per client) with a
  handler that sends `Content-Type: text/event-stream` and writes `data: …\n\n` + `flush()` in a loop.
- **Resume for free**: use the byte offset in `log.jsonl` as the event `id`. The browser's
  `EventSource` reconnects with `Last-Event-ID`, and the server seeks there and continues.
- **Change detection by polling** (`stat` on `log.jsonl` and the inboxes every 0.5–1 s). The stdlib
  has no inotify/FSEvents, and polling behaves the same on macOS and Linux.
- **Browser → server is plain `POST`** (send as human, ack an escalation). The dashboard is
  read-mostly, so a one-way stream plus POST covers it.
- WebSockets aren't in the stdlib. A hand-rolled RFC 6455 handshake and framing is ~150 lines, and
  bidirectional push buys nothing here.
- Localhost only: bind `127.0.0.1`. `http.server`'s "not for production" warning doesn't apply. The
  HTTP/1.1 limit of 6 connections per origin only bites with 7+ open dashboard tabs.

### CLI

All commands resolve the session as described in [Finding the session](#finding-the-session-from-any-pane);
`--as` defaults to `TANDEM_ROLE` inside a launched harness.

| Command | Purpose |
|---|---|
| `tandem init [name] [--ticket KEY] [--task TEXT \| --task-file F] [--replace]` | create the session tree and point the repo at it |
| `tandem send [--as R] --to R [--type T] [--reply-to ID]` | body from stdin or arg; tmp, mv to new, append to log |
| `tandem recv [--as R] [--wait] [--timeout S] [--peek]` | pop the oldest `new/` message to `cur/` and print it; `--peek` doesn't consume |
| `tandem watch [--as R] [--once]` | wait for mail and ring the harness doorbell (`--once`: exit 2 on mail, for Claude's asyncRewake hook) |
| `tandem log` | dump `log.jsonl` |
| `tandem status` | active sessions, pending counts per inbox, last message per role, stale pointers |
| `tandem done` | write the summary and clear the repo's active-session pointer |

## Waking an agent: the doorbell rule

An LLM session only acts when its harness gives it a turn, so notification is the hard part, not
transport. **The nudge is a doorbell, not the mail.** Whatever wakes an agent carries a fixed line
("run `tandem recv --as <role>`"), never message content. Lost or duplicate nudges are therefore
harmless, and the bus stays the single source of truth.

Wake paths are per harness; the protocol is shared.

### agy: `agentapi send-message` from inside agy's process tree (verified)

`agy agentapi send-message <conversation_id> <text>` posts a message into a running interactive
conversation. Tested 2026-11-10 on agy 1.5.4: with the TUI idle at its prompt, a send started a turn on
its own and agy answered within 5 s. Nothing was typed into the PTY. The injected message does not show
as a `>` prompt line in the TUI; only agy's reply is visible.

It only works with agy's environment (`ANTIGRAVITY_LS_ADDRESS`, `ANTIGRAVITY_CSRF_TOKEN`,
`ANTIGRAVITY_CONVERSATION_ID`). Every command agy spawns inherits it, and a `nohup` child keeps
working after the turn that launched it ends. From outside agy the call fails with
`ANTIGRAVITY_LS_ADDRESS is not set`.

Behaviour verified with a probe watcher (2026-11-10):

- **Mid-turn**: a send while agy is busy is **queued**, not dropped, and doesn't interrupt. A ring that
  landed before a `sleep 40` tool call started was answered after it, in the same turn.
- **Delivery form**: agy records the message as a `SYSTEM_MESSAGE` ("not actually sent by the
  user"), which is why it never shows as a `>` prompt line.
- **After `/new`**: the conversation ID changes but the LS address and token don't (same process). A
  watcher still holding the old ID rings the **old** conversation, which answers invisibly in the
  background. The watcher must follow the current conversation ID.

**Where the watcher can live.** Only processes agy spawns through `run_command` get the full env:

| Host | Gets LS env | Notes |
|---|---|---|
| Sidecar in a plugin | n/a | the CLI doesn't run sidecars: none started (not even the built-in swarm board) and `cli.log` never mentions them; they appear to be an Antigravity app feature |
| Hook (`PreToolUse` etc.) | **no**: only `ANTIGRAVITY_CONVERSATION_ID` | can't call `agentapi` itself |
| `nohup … &` from a `run_command` | **yes** | survives the turn; must run unsandboxed, because the sandbox blocks loopback to the LS |

Chosen design:

- agy starts `tandem watch --as <role>` itself via `run_command`, with the sandbox bypass forced by the
  tandem `PreToolUse` hook and approved by an `unsandboxed(...)` rule for the watch command, so it
  never prompts.
- A `SessionStart` hook injects a system message telling agy to start the watcher, so the model doesn't
  have to remember it from the skill.
- A `Stop` hook acts as watchdog: if the watcher's pidfile is stale, it returns `continue` with "start
  `tandem watch`". It also does the mail piggyback (`recv --peek`).
- Hooks know the current `conversationId` on every event, so they write it to the session dir and the
  watcher reads the target from there on each ring. That fixes `/new`.
- An agy restart changes the LS address and token, so a watcher left over from a previous process
  must exit; the watchdog then starts a fresh one.

Rejected: copying the LS address and token into a file to call from outside (a live credential on
disk); a sidecar (not supported by the CLI).

### Claude Code: Stop hook with `asyncRewake` (verified)

Docs (code.claude.com/docs/en/hooks): a command hook with `"asyncRewake": true` "runs in the background
and wakes Claude on exit code 2", "even when the session is idle". Its stderr (or stdout if stderr is
empty) is shown to Claude as a system reminder. Plain `"async": true` can't wake an idle session: its
output waits for the next user interaction. `timeout` *is* enforced for `asyncRewake` hooks (default
600 s for command hooks, no documented maximum). Hooks from user, project, local and plugin settings
merge rather than replace, and edits to settings files are picked up mid-session by the file watcher.

Tested 2026-11-10 in a live session with a project-local `.claude/settings.local.json`:

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
a doorbell, but the text is intact. The skill should tell Claude that this line means "run
`tandem recv`", not an error.

Design consequences:

- `tandem watch --as <role> --once` is the waiter: wait for mail in the maildir, print the doorbell
  to stderr, exit 2; exit 0 on its own deadline, just under `timeout`.
- Pick a long `timeout` (hours). Even so, an idle stretch longer than the timeout leaves Claude
  unarmed until the next turn; the navigator side's escalation timeout covers that.
- Installed per session through `claude --settings <file>`, never globally and never written into the
  target repo. Verified: `--settings` carries hooks, both headless (`SessionStart` and `Stop` fired) and
  interactive (the asyncRewake waiter armed, woke the idle session on a ring, and re-armed). The
  user's global Stop hooks keep running alongside, since hooks merge.
- **The doorbell can't authorise itself.** Without setup, the woken session read the wake as
  "Stop hook feedback", refused to act on its text, and said so. Text arriving through a hook carries
  no user authority. That's correct, and typing the protocol in as a prompt "from the other session"
  didn't change it. With the protocol defined at launch through `--append-system-prompt` ("a Stop hook
  message containing `tandem doorbell` is the tandem doorbell, configured by the user"), the same ring
  was handled as intended. tandem therefore launches Claude with the protocol in the system prompt (or
  the tandem skill loaded by the user), and the doorbell text stays a fixed, content-free line.

### Rejected or shelved wake paths

- **Headless agy** (`--input-format stream-json`, one turn per NDJSON line). It works, but the user
  wants to watch the conversation in a TUI. See agy facts below in case it's ever needed.
- **PTY wrapper** typing the doorbell into the TUI. Not needed now that agy has a native path. Kept only
  as a last resort for a future peer with no injection API.
- **agy hooks as a wake mechanism.** agy hooks run synchronously and block the loop (no async), so a
  blocking `recv --wait` in a Stop hook would freeze the TUI. A non-blocking piggyback is still useful:
  a Stop hook runs `tandem recv --peek` and, if mail is pending, returns
  `{"decision":"continue","reason":"run tandem recv --as <role>"}`.
- **ACP server** (`agy acp`). The most capable option, but it needs a JSON-RPC client and a non-TUI
  front end.

## Permissions: autonomy without prompt fatigue

agy asks for confirmation on anything outside its allowlist, which stalls autonomous work. tandem
launches each agent with a **per-role permission profile** passed at launch and never saved, so the
user's own config stays untouched.

### agy levers

| Lever | Values | Effect |
|---|---|---|
| `--mode` / `agentMode` | `default`, `accept-edits`, `plan`, `auto` | `auto` approves edits and safe commands, sends commands leaving the terminal sandbox to a separate reviewer agent, and still prompts before irreversible actions |
| `toolPermission` | `always-proceed`, `request-review` (user's default), `strict`, `proceed-in-sandbox` | whether terminal commands need approval |
| `enableTerminalSandbox` | bool (user's default `false`) | restricted environment for commands |
| `permissions.allow` / `deny` | `command(prefix)`, `read_file(path)`, `write_file(path)`, `execute_url(domain)`, `mcp(server/tool)` | rules at three scopes: project, shared (`~/.gemini/config/config.json`), global (`settings.json`); deny rules still apply under `--dangerously-skip-permissions` |
| `PreToolUse` hook | `allow`, `deny`, `ask`, `force_ask` per call | programmable policy over each tool call's JSON |
| `--settings <json\|file>` | any settings keys | per session, never saved: the carrier for tandem's profiles |
| `--dangerously-skip-permissions` | none | rejected: too blunt, especially for a driver |

### Decision

- **Navigator profile: sandboxed, with bypass rules for the project's test commands.**

  ```json
  {
    "toolPermission": "proceed-in-sandbox",
    "enableTerminalSandbox": true,
    "permissions": {
      "allow": ["unsandboxed(go test)", "unsandboxed(dip rspec)", "unsandboxed(bundle exec rspec)",
                "unsandboxed(tandem)"],
      "deny": ["write_file(*)"]
    }
  }
  ```

  Everything except tests and `tandem` runs read-only with no prompts; tests run on the host for
  real. The test prefixes are per project: tandem derives them from the repo (`go.mod` → `go test`,
  `dip.yml` → `dip rspec`, ...) or reads them from a small per-repo tandem config.
- **The tandem `PreToolUse` hook forces the bypass** for `run_command` calls matching a test prefix
  or `tandem`: it returns `{"decision":"allow","overwrite":{"BypassSandbox":true}}`, and
  `{"decision":"ask"}` (the pass-through) for everything else. An `unsandboxed` rule only approves a
  bypass the agent asks for, and the hook alone still prompts, so both are required (verified).
- **Fallback profile (option 1)**, used if `unsandboxed` rules stop working: no sandbox,
  `request-review`, a tight allowlist (read-only git, `tandem`, the exact test commands such as
  `command(dip rspec)`, never `command(dip)`), `write_file(*)` denied, and the hook gating shell writes.
  tandem checks at startup that a bypass rule still works and switches to this profile if it doesn't.
- **Driver profile (when agy drives)**: `--mode auto` plus the tandem and git allowlist, no sandbox
  (the sandbox makes the workspace read-only, so a sandboxed driver can't work). Prompts are left only
  for irreversible actions, which the user wants to see anyway.
- **Safety net for any profile**: the driver runs `git status` at each checkpoint and treats any change
  it didn't make as a navigator violation. Tests legitimately write gitignored paths (`coverage/`,
  `log/`, `tmp/`), which `git status` already ignores.
- The doorbell never prompts: agy's `tandem watch` is started once through `run_command` with the
  bypass forced by the hook and approved by `unsandboxed(tandem)`, and each ring is an `agentapi`
  call from that watcher, not a tool call.
- Claude gets the same treatment: a per-session settings profile (allow/deny rules) plus a hook.

### Verified (2026-11-10, agy 1.5.4)

Profile `{"permissions":{"allow":["command(date)"],"deny":["write_file(*)"]}}` via `--settings`:

| Probe | Prompted | Finding |
|---|---|---|
| `date` | no | `--settings` permissions apply |
| `date -u` | no | `command(x)` is a prefix match; arguments pass |
| `date; touch f` | yes (offered to allow `touch`) | compound commands are split and each part checked |
| `date && touch f` | no, but only because `touch` had been allowed for the conversation one step earlier | `&&` is split like `;` |
| `date \| tee f` | yes (offered to allow `tee`) | pipes are split too |
| `ls` (allowed only in global settings) | yes | `--settings` permissions **replace** the global list, not merge: the profile is the whole policy |
| `whoami` | yes | baseline |
| file tool writes `notes.txt` | denied, no prompt | `write_file(*)` blocks the file tool ("Matches user-configured deny rule") |

Consequence: `write_file(*)` covers only agy's file tool. The shell is a second write channel
(`touch`, `tee`, redirects, `sed -i`, ...). How each profile closes it:

- **Navigator profile (sandbox)**: the sandbox makes the workspace read-only, so shell writes fail
  ([Sandbox findings](#sandbox-findings-2026-11-10-agy-154)). Only the bypassed prefixes (tests,
  `tandem`) run outside it, which is why the hook matches exact prefixes and never `dip` alone.
- **Fallback profile (allowlist)**: anything unlisted prompts, so shell writes can't pass silently.
- **Either way**, git is the safety net: the driver checks `git status` at each checkpoint and treats
  any change it didn't make as a navigator violation.

### Sandbox findings (2026-11-10, agy 1.5.4)

Profiles passed with `--settings`; all checked first with `agy --settings <file> -p=/config`, which
shows the effective config without spending a model turn. `--settings` accepts `toolPermission`,
`enableTerminalSandbox` and `unsandboxed(...)` rules as well as `command`/`write_file` rules. The
`--sandbox` flag alone doesn't show up in `/config`; tandem uses `--settings`.

With `toolPermission: proceed-in-sandbox` and `enableTerminalSandbox: true`, **nothing prompted**, and:

| Action | Result |
|---|---|
| write in the workspace (`touch`, `echo > f`) | blocked, **even with no deny rule**: the sandbox makes the workspace read-only |
| write outside the workspace | blocked |
| read outside the workspace (`ls ~/code/tmp`, `~/.gitignore`, `~/.docker/config.json`) | blocked |
| network (`curl https://example.com`) | blocked (DNS fails) |
| loopback TCP (`httptest` servers) | blocked: `connect: operation not permitted` |
| Docker socket (`docker ps`) | blocked: `permission denied ... /var/run/docker.sock` |
| `dip` (rbenv shim) | blocked from executing at all |
| `git status`, `python3 -c`, `go vet ./...` | ran (the Go toolchain reaches its caches outside the repo) |
| `go test ./...` in apix | ran, but the 3 packages using `httptest` failed on loopback |

Allow rules do **not** take a command out of the sandbox: `command(go test)` still ran sandboxed.

**Bypass.** agy can ask to run a command outside the sandbox ("Allow sandbox bypass for command
execution? ... full network and disk access"). The prompt offers run once, always allow for the
conversation, or persist. Persisting writes `unsandboxed(<prefix>)` to the global `settings.json`. A
profile with `unsandboxed(go test)` then gave:

| Command | Bypass requested | Result |
|---|---|---|
| `go test -count=1 ./cmd/apix/` | no | ran **inside** the sandbox (`BypassSandbox: false`), FAIL on loopback |
| `go test -count=1 ./internal/pipeline/` | yes | ran **outside**, approved without a prompt: `ok` |

So the short prefix matches, `--settings` carries the rule, and the rule approves but does not force a
bypass. agy's startup warning calls `unsandboxed(...)` rules **deprecated** and says to migrate them to
`command(...)`, but `command(...)` rules don't bypass. `unsandboxed` is the only working mechanism today
and may disappear in a future release. The 1.5.5 notice says such rules are ignored; whether that's
literal is open ([Open questions](#open-questions-and-next-experiments), item 4).

Implication for Ruby apps: `dip rspec` (Docker + dip) can't run in the sandbox at all, only bypassed
(verified below).

**Hook contract (verified).** A `PreToolUse` hook on `run_command` must return an explicit decision:
`"decision": ""` and `{}` both **deny** the call. `{"decision":"ask"}` defers to the normal flow (no
prompt under `proceed-in-sandbox`), so it's the pass-through. Tool args seen by the hook:
`CommandLine`, `WaitMsBeforeAsync`, `toolSummary` (no `BypassSandbox` key unless one was requested).

**Forcing a bypass (verified).** `{"decision":"allow","overwrite":{"BypassSandbox":true}}` turns the call
into a bypass request. With a matching `unsandboxed(...)` rule it runs outside the sandbox without a
prompt (loopback test passed). **Without the rule, agy still prompts for the bypass**: hook `allow` does
not approve it. Both are needed, so the dependency on the deprecated `unsandboxed` rule kind stays.

**`dip rspec` through a bypass (verified).** In a Ruby app with `unsandboxed(dip rspec)` and a
requested bypass: no prompt, containers started, `5 examples, 0 failures`, `git status` clean
afterwards (`coverage/` is gitignored). The command still exited 2, because SimpleCov's minimum
coverage fails on a single-file run. When the navigator runs a subset of specs, it judges by the
examples/failures line, not the exit code.

**`SessionStart` and `/new` (verified).** `SessionStart` fires for the new conversation, with its new
`conversationId`, when the first message after `/new` arrives, just before the first `PreInvocation`.
Between `/new` and that message the old ID is still current, so a ring in that window goes to the old
conversation. That's harmless under the doorbell rule: the mail stays in the maildir, and the new
conversation's `SessionStart` hook can inject "you have pending mail, run `tandem recv`".

## Protocol rules (for the `tandem` skill)

- Phases: intake (ticket, `--task` brief, or the user's first prompt), plan, plan review, checkpointed implement + review, summary.
- **Bounded consensus.** The navigator must state risks and its strongest objection before it can
  approve. 2–3 rounds at most, then `escalate` to `human`. "Loop until agreed" is rejected because
  LLMs cave or loop.
- **Checkpointed review**, per plan step or commit, not continuous parallel review that chases a moving
  tree.
- Skip review for trivial changes.
- A peer agreeing is never user approval.
- A reply is never promised: a model can ignore a message that arrived fine.
- Summary is built from `decisions.md` plus `log.jsonl`.
- The skill never touches transport details. Only a thin per-peer adapter does, so peers can be
  swapped.

## agy facts (1.5.4, config re-checked on 1.5.5)

The behaviour above (sandbox, bypass, hooks, `agentapi`) was verified on agy 1.5.4. On 1.5.5 only
what print-mode slash commands can show without a model turn has been re-run (2026-11-11, see
[agy 1.5.5 re-check](#agy-155-re-check-2026-11-11)); its changelog doesn't mention the sandbox,
`unsandboxed` rules or `agentapi`.

- Config: `~/.gemini/antigravity-cli/settings.json` (permissions `allow`/`deny` with `command(...)`
  rules, `trustedWorkspaces`); shared hooks in `~/.gemini/config/hooks.json`; per-workspace hooks in
  `<workspace>/.agents/hooks.json`.
- Hook events: `SessionStart`, `PreToolUse`, `PostToolUse`, `PreInvocation`, `PostInvocation`, `Stop`.
  Command hooks only, JSON on stdin and stdout, 30 s default timeout, synchronous, run via `sh -c`
  with the directory holding `hooks.json` as cwd. Docs ship in the binary's builtin skill:
  `~/.gemini/antigravity-cli/builtin/skills/agy-customizations/docs/hooks.md`. Every payload carries
  `conversationId`, `lastUserInput`, `hookEventName` and `cwd` (the agent's working directory).
- **`hooks.json` top-level keys are hook names, not events** (verified on 1.5.5 with `-p=/hooks` and
  `--add-dir`): `{"tandem":{"PreToolUse":[{"matcher":"run_command","hooks":[{"command":"…"}]}],
  "SessionStart":[{"command":"…"}],"PreInvocation":[…],"Stop":[…]}}` loads four handlers under the
  name `tandem`. The same arrays with event names at the top level load **nothing**, silently.
  `PreToolUse`/`PostToolUse` are grouped (`matcher` + `hooks`); the other events are flat lists of
  handlers.
- **No-op outputs** (from `hooks.md`; the protojson decoding makes a missing field its zero value):

  | Hook | No-op | Why |
  |---|---|---|
  | `PreToolUse` | `{"decision":"ask"}` | `{}` and `"decision":""` deny (verified on 1.5.4) |
  | `SessionStart` | `{}` | `injectSteps` is optional |
  | `PreInvocation` | `{}` | `injectSteps` optional; `terminationBehavior` omitted or `""` proceeds to the model |
  | `Stop` | `{}` | only `"decision":"continue"` blocks the stop; "any other value allows the agent to stop" |
  | `PostToolUse` | `{}` | documented as the expected output |

  The non-`PreToolUse` rows come from the docs, not from a live run; Task 17 exercises them.
- **Initial prompt in interactive mode**: `-i` / `--prompt-interactive <prompt>` "runs an initial
  prompt interactively and continues the session" (`agy --help`, 1.5.5). There is no positional
  prompt. Launchers use `--prompt-interactive=<text>` (the `=` form, as with `-p=`). Not run live
  yet, since it spends a model turn.
- `--mode accept-edits|auto|plan`, `--settings <json|file>` (per session, never saved), `--sandbox`.
- Messages typed while agy is busy are queued ("Send Immediately" queue mode exists).
- `agy -p "/hooks"` and other read-only slash commands answer without spending a model turn.
- Headless stream-json, for reference: `agy --input-format stream-json --output-format stream-json -p=`.
  `-p` takes its value, so `-p=` must come last. Input line:
  `{"event":"user","message":{"role":"user","content":"..."}}`. Output: `init`, `step_update`, then one
  `result` per turn. Context carries across turns.

### agy 1.5.5 re-check (2026-11-11)

All with print-mode slash commands (`num_turns: 0`), so no model turn was spent.

- `agy --version` → `1.5.5`.
- The navigator profile (`proceed-in-sandbox`, `enableTerminalSandbox: true`, `unsandboxed(go test)`,
  `unsandboxed(dip rspec)`, `unsandboxed(tandem)`, deny `write_file(*)`) loads: `-p=/config
  --output-format json` shows all three keys as written, `-p=/permissions` lists the rules at global
  scope, and the log line `CLI settings initialized: permissions=&{Allow:[unsandboxed(go test) …]}`
  appears. The fallback and driver profiles load the same way.
- **`/config` and `/permissions` echo rule strings without validating them**: a profile with
  `bogus(x)` shows `bogus(x)` too. They prove a profile parsed and replaced the global list; they
  can't prove agy honours a rule kind. A startup self-check built on `/config` alone only catches a
  profile agy fails to parse.
- **`--settings` replaces `permissions.allow` and `permissions.deny` separately.** A profile without
  a `deny` key kept the global `deny` (`command(rm -rf)` appeared in `/config`). Generated profiles
  therefore always write both keys, so the profile stays the whole policy.
- `--mode auto` doesn't show in `/config` (`agentMode` stays `""`), like `--sandbox`. It can only be
  confirmed live.
- **No `unsandboxed` warning in print mode**: nothing on stdout, stderr or `--log-file`. The warning
  is a TUI startup notice. Its 1.5.5 text, taken from the binary's strings since it can't be
  captured without starting the TUI:
  - `Invalid "unsandboxed" permission rules found; they are ignored and grant nothing:` (followed
    by the offending files and rules)
  - `Fix: replace "unsandboxed" with "command" (e.g. command(git status)), or remove the rule. Edit the file directly or use /permissions.`
  - `unsandboxed is no longer a valid rule, use command() instead`

  On 1.5.4 the notice was read as "deprecated" and the bypass still worked. The 1.5.5 wording says
  the rules are **ignored**, and the changelog doesn't mention the change. If that's literal, the
  navigator's default profile no longer bypasses the sandbox on 1.5.5 and the fallback profile has
  to become the default. Only a live run can tell (Task 17, first check).
- The model-facing sandbox description in the 1.5.5 binary says standard sandbox mode "has full
  read/write access to your workspace", while on 1.5.4 a sandboxed `touch` in the workspace was
  blocked. Also live-only; if the workspace became writable, the navigator's single-writer rule
  rests on `write_file(*)` plus the driver's `git status` check, which don't close the shell channel.

### Claude Code permission rules (docs, 2026-11-11)

From code.claude.com/docs/en/permissions and /settings:

- **Prefix rule: `Bash(tandem *)`.** A trailing ` *` also matches the bare command (`tandem`), and
  the space is part of the rule, so `tandemx` doesn't match. `Bash(tandem:*)` is an equivalent older
  spelling; the permission dialog itself writes the space form, so tandem does too.
- Compound commands (`&&`, `||`, `;`, `|`, `|&`, `&`, newline) need every part allowed. A leading
  assignment of a variable outside a known-safe set stops an allow rule matching
  (`TANDEM_ROLE=x tandem send` would prompt), and a rule matches the command text, so the protocol
  tells agents to call plain `tandem …`, not the shim by absolute path.
- A bare tool name in `deny` (`"Edit"`, `"Write"`, `"NotebookEdit"`) removes the tool from Claude's
  context entirely, which is what the navigator wants. Deny beats allow at every level.
- `--settings` sits above user, project and local settings, and **lists merge across scopes**
  (unlike agy): the user's own allow rules still apply inside a tandem session, and the navigator's
  deny rules can't be undone by them.

## Open questions and next experiments

None blocking the code. Settle while building v0:

1. Whether agy's `lastUserInput` on `PreInvocation` distinguishes a human prompt from a
   `send-message` doorbell (both start a turn). The human-input log hook must not record doorbells
   as human messages.
2. Whether `plan` mode blocks running tests (low priority: the sandbox does the read-only job).
3. ~~Re-check the navigator profile on agy 1.5.5 without a model turn~~: done, see
   [agy 1.5.5 re-check](#agy-155-re-check-2026-11-11). The profile loads, but print mode can't show
   whether `unsandboxed(...)` still works, which became item 4.
4. **Does agy 1.5.5 still honour `unsandboxed(...)`?** Its startup notice says the rules are
   "ignored and grant nothing". First live check in Task 17: the navigator profile running
   `tandem status` with the hook forcing `BypassSandbox`. If it prompts, make the fallback profile
   the default and keep the sandboxed one behind `--profile`.
5. **Is the workspace still read-only inside the 1.5.5 sandbox?** Live check in Task 17: sandboxed
   `touch` in the repo. If it's writable, the sandboxed navigator profile loses its main guarantee.
6. What the `tandem agy` self-check can test. `/config` doesn't validate rule kinds, so it can only
   confirm the profile parses. The honest signal is the agy version: record versions where Task 17
   passed and treat any other version as unverified (warn, or fall back).
