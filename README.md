# tandem

A pairing protocol for two coding agents working one task together. Claude Code and Antigravity CLI
(`agy`) come first; Copilot or Codex can be swapped in later. Typical flow: the user hands over a task
(a Jira ticket, a brief, or just a first prompt), the driver writes a plan, the navigator challenges
it, they converge or escalate to the human, the driver implements in checkpoints while the navigator
reviews each one, and they finish with a summary of what was done and which decisions were made and
why.

Status: v0 implemented (Python 3.9+, stdlib only), unit- and integration-tested on Python 3.9 and
3.15 against fake harnesses. Live checks in real Claude and agy TUIs are still pending (plan Task 17;
see [Open questions](#open-questions-and-next-experiments)). This file is the decision record;
`AGENTS.md` is for working on tandem itself.

## Install and usage

Needs Python 3.9+, `git`, and `claude` and/or `agy` on PATH. No dependencies to install.

```sh
git clone <tandem repo> && cd tandem
make install   # symlinks bin/tandem into ~/.local/bin (BINDIR=... elsewhere); make uninstall undoes it
make test      # the suite on python3 and /usr/bin/python3
```

A typical session, in the target repo on the task's branch:

```sh
tandem init --ticket APIX-123   # or a name, --task "…", --task-file brief.md; once per task
tandem claude                   # terminal 1: Claude drives
tandem agy                      # terminal 2: agy navigates
tandem human                    # optional, any terminal: escalations in, answers out
```

Give the task in the driver's console if `init` didn't get one. The pair plans, reviews and
implements on their own; type to either console at any time. `tandem status` and `tandem log -f`
show where things stand. When the work is done the driver writes its wrap-up, asks you, and runs
`tandem done`, which writes `summary.md` into the session dir. `--role` on a launcher flips the roles.

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
  for Edit, Write and NotebookEdit (its Bash isn't gated yet, see the backlog). agy as navigator: a `write_file(*)` deny plus the tandem `PreToolUse` write
  gate on shell commands, because agy 1.5.5's sandbox no longer makes the workspace read-only and
  from 1.5.6 the navigator runs without a sandbox; see
  [Permissions](#permissions-autonomy-without-prompt-fatigue).
- The navigator reviews evidence (diffs, commits, test runs it ran itself), not chat claims.

## Transport: maildir per session, no daemon

Sessions live in a **global spool**, `~/.tandem/sessions/<session>/`, not in the target repo. That
keeps the bus out of the repo's git state, and both launchers attach it to the harness's workspace
with `--add-dir`. `TANDEM_HOME` moves the whole spool (default `~/.tandem`). tandem creates the
spool's directories owner-only (0700): the log holds every prompt typed into a console.

### Naming

The session name is only a label for addressing; the ticket is separate, optional input for intake.

| Command | Name |
|---|---|
| `tandem init` | derived from the branch (`feature/login-timeout` → `login-timeout`); on detached HEAD, `main` or `master`, `<repo>-<MMDD-HHMM>` |
| `tandem init fix-csv-bom` | any free-form name |
| `tandem init --ticket APIX-123` | defaults to the ticket key; the ticket is stored for intake |

- Names are sanitized to `[a-z0-9-]` on disk (NFKD first, so `Café` becomes `cafe`; capped at 48
  characters); the original string is kept in `session.json`.
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
| 2 | `TANDEM_SESSION` | `tandem send/recv/watch` typed by the agent inside a launched harness (generated hooks pass `--session <dir>`, step 1) |
| 3 | the active session for the current repo (`git rev-parse --show-toplevel`, so subdirectories work) | launchers, the normal case |
| 4 | the only active session on the machine | `tandem human` outside any repo |
| 5 | otherwise: error listing the active sessions | ambiguity is never guessed |

- **Worktrees** have different toplevels, so parallel tasks in worktrees get separate sessions for free.
- Step 4 applies only outside a git repo. Inside a repo (or a worktree) without its own active session,
  taking the machine's only session would attach a launcher to another tree's plan, so it is an error.
- Steps 1 and 2 return an ended session too; each command decides whether that is an error (hooks and
  watchers exit quietly on one). A `--session` short name matches the current repo's sessions first,
  then all sessions; among several matches, the single active one wins.
- Anything waiting on a session (`recv --wait`, `log -f`, both watchers) stops when it ends, is
  removed, or its `session.json` stops parsing: `send` refuses such a session, so no more mail can
  arrive. `session.json` is replaced atomically, so a reader never sees it half-written.
- **One active session per checkout.** `tandem init` refuses while one is active unless `--replace`:
  one working tree can't take two drivers anyway. `--replace` ends the old session without a
  summary (its `session.json` gets `ended` and `replaced_by`, its log a `replaced` event); run
  `tandem done` first to keep its `summary.md`. A pointer to an unreadable `session.json` counts as
  stale, like an ended or missing one.
- **Ending.** `tandem done` writes the summary and clears the repo pointer. A stale pointer (one naming
  an ended or missing session) shows in `tandem status`; a plain `tandem init` takes over from it.
  After `done` the repo resolves to no session, so an ended session's `log`/`status` need `--session`.
- `summary.md` is mechanical: session facts, task, a one-line-per-message timeline, every console input
  in full, the bodies of `done` messages, then `decisions.md` verbatim. The narrative is the driver's,
  in `decisions.md` or its `done` message, written before it calls `tandem done`. `done` takes the
  session lock that `send` takes, so no message can land after the summary is written.
- `init` stays explicit in v0; launchers don't auto-init.

```
~/.tandem/sessions/<session>/
  session.json       # name as typed, repo path, ticket/task, test_cmds, roles, claims, created/ended
  lock               # the session flock: ids, log appends, role claims, rings, `done`
  peers/claude/      # settings.json (hooks, permissions) and protocol.md from `tandem claude`; hooks.log
  peers/agy/         # profile.json from `tandem agy`; conversation_id and hook state; hooks.log
  peers/<role>/      # watcher state: watch.pid, rung, watch.log (agy's detached watcher)
  .agents/           # hooks.json and rules/AGENTS.md; loaded via `agy --add-dir <session-dir>`
  inbox/driver/{tmp,new,cur}/
  inbox/navigator/{tmp,new,cur}/
  inbox/human/{tmp,new,cur}/
  log.jsonl          # append-only, one JSON record per line: `message` (every envelope), `console`
                     # (human input typed into a harness), `event` (init, replaced, role_claimed,
                     # agy_profile, watcher_started, watcher_exited, done); input for the summary
  plan.md            # driver-owned
  decisions.md       # driver-owned; the summary derives from it
  summary.md         # written by `tandem done`; agents write decisions.md, not this
```

- Delivery: write to `tmp/`, then `mv` to `new/`. The reader never sees a partial message.
- Ack: move from `new/` to `cur/`. Crash recovery is free.

### The human

The user takes part two ways, and both are first-class:

- **Typing into either agent's console** (usually the driver's). This is the natural one, but it
  bypasses the bus, so each launcher installs a hook that appends every prompt the user types to
  `log.jsonl` as `from: human`: `UserPromptSubmit` for Claude, `PreInvocation`'s `lastUserInput` for
  agy. Decisions made in a console then still reach the summary.
- **`tandem human`** from any terminal, even outside the repo (resolution step 4): reads the `human`
  inbox (escalations, questions) and sends to either role. A message without a type answers the last
  message the user read from that role, so a reply to an escalation threads without typing an id.

No separate notifier. An agent that escalates also says so in its own console, so the user sees it
where they are already looking; the protocol requires it.
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
`expects_reply` plus a `recv --timeout` after which the waiting agent escalates to `human` prevents
mutual-wait deadlock. `recv` itself never escalates: on timeout it exits 3, and the protocol tells the waiting agent to
escalate, so the escalation carries the agent's own account of what it is waiting for.

- Ids are per session, shared by all three inboxes, gap-free: the next id is one above the highest
  message in any `new/` or `cur/`, allocated under the session's `flock`, so `reply_to` names one
  message whichever inbox it sits in. `--reply-to` must name an existing message.
- `--type` defaults to `answer` with `--reply-to`, else `question`. A reply that defaulted to
  `question` would expect a reply back, and two agents would answer each other's answers forever.
- `--commit` is resolved in the session's repo and stored as a 12-char sha, so `HEAD` keeps meaning
  the commit it named when the message was sent. `--file` paths are stored as given.

### Launchers: `tandem <peer>` is the per-peer adapter

Each peer needs a specific launch: a permission profile, hooks, the doorbell protocol in its
instructions, session env, and for agy a startup self-check. The launcher sets all of that up and then `exec`s
the real binary, so the harness owns the terminal directly (no wrapper process, no PTY). tandem never
opens panes: the user runs each launcher in whatever terminal they like.

| Command | Does |
|---|---|
| `tandem claude [--role driver\|navigator]` | writes per-session Claude settings (Stop hook with `asyncRewake`, `UserPromptSubmit` human-input log, permissions) into the session dir; `exec claude --add-dir <session-dir> --settings <file> --append-system-prompt <protocol> [extra] -- <tandem: prompt>` |
| `tandem agy [--role navigator\|driver] [--profile auto\|fallback]` | picks the navigator profile (fallback on agy 1.5.6 and later, otherwise the self-check decides), writes the agy profile (`command(...)` allowlist, or on 1.5.4–1.5.5 sandbox + `unsandboxed(<test cmd>)`, for the navigator; `--mode accept-edits` and `write_file(...)` for `plan.md`/`decisions.md` for a driver) and the hooks/rules into the session dir; `exec agy --settings <file> --add-dir <session-dir> [--mode accept-edits] --prompt-interactive=<tandem: prompt> [extra]` |
| `tandem human` | small REPL: tails the `human` inbox (escalations, questions) and sends to either role |
| `tandem dash` (later) | web dashboard over the session dir (`log.jsonl`, `plan.md`, `decisions.md`, inboxes); not designed yet, feasibility noted in [Dashboard](#dashboard-tandem-dash-later-stdlib-is-enough) |

- Defaults follow the user's preference: Claude drives, agy navigates. `--role` flips them, and the
  launcher picks the matching profile.
- **Role claims.** A launcher records `claims.<peer> = {role, pid, ts}` in `session.json` and logs a
  `role_claimed` event before it `exec`s. `exec` keeps the pid, so a claim lives exactly as long as
  the harness. `--role` taking the other peer's role swaps the two in `roles`, unless that peer has a
  live claim (then it's an error: quit it first). A dead claim is simply released, so relaunching
  after a crash never deadlocks. A peer launched without `--role` takes the role `roles` gives it.
- `tandem claude` adds the session dir with `--add-dir`, so Claude reads `plan.md`, reviews and the
  log (and the driver edits `plan.md`/`decisions.md`) without asking for an outside-workspace path.
  Claude's own arguments go after `--` (`tandem claude -- --model opus`); tandem puts its initial
  `tandem:` prompt after a second `--`, so a variadic flag such as `--allowed-tools` can't swallow it.
- Launchers export `TANDEM_SESSION` and `TANDEM_ROLE`, so hooks and `tandem send/recv` know who they
  are without flags, and `cd` to the repo's toplevel before `exec`, so the harness's workspace is
  the whole checkout even when the launcher ran in a subdirectory.
- **Protocol parity.** Both peers get the doorbell protocol and role rules from launch-time,
  user-authored instructions rendered from one template: Claude through `--append-system-prompt`
  (copy in `peers/claude/protocol.md`), agy through `.agents/rules/AGENTS.md` in the session dir,
  loaded with `--add-dir`. agy followed doorbell text in the probes without setup, but parity means
  we don't rely on that. No tandem skill or agy plugin ships: launch-time instructions already reach
  both peers with user authority, and a skill would be one more thing to install.
- **agy customizations ship in the session dir via `--add-dir` (verified).** From an added dir, agy
  loads `.agents/hooks.json`, `.agents/rules/AGENTS.md`, a root `AGENTS.md`, `.agents/skills/<name>/`,
  and plugins under `.agents/plugins/<name>/` with their own hooks, rules and skills (plugin skills
  are namespaced `<plugin>:<skill>`). Checked with print-mode `/hooks`, `/skills` and `/plugin list`
  (no model turn), and for rules with marker words agy reported from context, with no tool calls in
  the event stream. Nothing is installed globally and nothing is written into the target repo.
- **What `tandem agy` writes** (`tandem/launch_agy.py`): `.agents/hooks.json` with one hook name,
  `tandem`, holding `PreToolUse` (a `run_command` matcher group), `SessionStart`, `PreInvocation` and
  `Stop`, each calling `'<shim>' --session <dir> hook agy-…`; `.agents/rules/AGENTS.md`, the rendered
  protocol for agy's role (refused if over agy's 24 KB rules cap); and `peers/agy/profile.json`, one
  of three profiles (see [Decision](#decision)). The test prefixes come from `session.json`
  `test_cmds` (whitespace collapsed and duplicates dropped at `init`); one containing `(` or `)` is
  left out of the rules, since it would end the rule early, and the hook and Claude's allow rules
  leave it out too, so all three agree. Generated hooks.json and all three profiles were loaded with
  print-mode `/hooks` and `/config` on 1.5.5 (no model turn).
- **Each profile's `deny` carries the user's own.** A `deny` in `--settings`, even `[]`, replaces
  the global one, so tandem writes its own deny rules followed by the user's global agy deny
  (`~/.gemini/antigravity-cli/settings.json` `permissions.deny`, read and never written,
  deduplicated). A user's `command(rm -rf)` stays in force under every profile. The user's global
  `allow` is not carried over: the profile's allowlist is the whole allowlist.
- **agy 1.5.6 and later get the fallback profile (`tandem agy`, navigator, `--profile auto`).**
  `agy --version` comes first. From 1.5.6 (`UNSANDBOXED_REMOVED_IN`), `unsandboxed(...)` rules are
  gone and every sandbox bypass asks the user ([agy 1.5.6 live check](#agy-156-live-check-2026-11-12)),
  so the sandboxed navigator profile would prompt on each tandem command. The launcher prints a
  one-line note, uses the fallback profile and runs no self-check, which couldn't tell (`/config`
  still echoes the rules). An `agy --version` that isn't a plain `x.y.z` keeps the self-check path.
- **Self-check (`tandem agy`, navigator, `--profile auto`, agy before 1.5.6).** `agy --version`, then
  `agy --settings <navigator profile in a temp dir> -p=/config --output-format json` (30 s timeout,
  no model turn). It passes when the printed config shows the profile's `toolPermission`,
  `enableTerminalSandbox` and every allow and deny rule, and agy says nothing about `unsandboxed`
  being unsupported. That proves only that the profile parses (`/config` doesn't validate rule
  kinds), so the version carries the rest: on a version in `VERIFIED_AGY_VERSIONS` (1.5.4, 1.5.5,
  where live sessions confirmed the bypass) the navigator profile is used silently; on any other
  version that parses it is still used (the user's choice of sandbox + write gate), with a one-line
  warning that the bypass is unverified and a pointer to `--profile fallback`. A failed check
  switches to the fallback profile with a one-line warning. A definitive result (the config loaded
  as written, a setting or rule lost, or agy's "unsupported" notice) is cached in
  `$TANDEM_HOME/agy-check.json` keyed by `agy --version`; a timeout, a non-zero exit or output that
  isn't a config (an expired login, an update banner, a first-run prompt) is re-checked on the next
  launch. A warning from a cached failure names the file to delete to re-run the check. The launch
  logs an `agy_profile` event with the profile used. `--profile fallback` skips the check; a driver
  never runs it, and `tandem agy --role driver --profile fallback` warns that the flag is for the
  navigator and uses the driver profile.
- `tandem agy` starts agy with `--prompt-interactive=<tandem: line>`, which gives agy a first turn in
  the TUI, so `SessionStart` fires and tells it to start its watcher; `agy-preinvocation` skips the
  line. A driver also gets `--mode accept-edits` (`auto` needs the terminal sandbox, which the
  driver profile turns off, so agy silently fell back to the default mode and asked before every
  edit; [flipped-roles run](#agy-156-live-check-2026-11-12)). agy's own arguments go after `--`.
- agy's `run_command` runs in the session dir (the `--add-dir`), not the repo the launcher `cd`s
  to. Live on 1.5.6: `pwd` printed the session dir with the protocol naming the repo, with a second
  `--add-dir <repo>` before or after it (the conversation lists the session dir as the first
  workspace root either way), and with a `PreToolUse` `overwrite` setting `Cwd` to the repo (agy
  reported the change and ignored it; its `run_command` takes no `Cwd`). Each command gets a new
  shell, so a `cd` doesn't carry over. The protocol therefore has agy run repo commands as
  `cd <repo> && <command>`, which agy's rules approve part by part (`cd` is on agy's own system
  allowlist) and the write gate still checks; tandem commands stay plain, since they work from any
  directory. Under the sandboxed 1.5.4–1.5.5 profile such a compound command gets no bypass.
- Consequence (sandboxed navigator profile, agy 1.5.4–1.5.5): the tandem commands agents run must
  run outside the sandbox, the same as tests. The
  sandbox blocks reads outside the workspace, and `tandem` needs the shim and package from its
  checkout plus `~/.tandem/repos/`. On 1.5.4 the session dir itself was read-only inside the sandbox
  too. Only the subcommands the protocol has agents type are bypassed: the hook forces
  `BypassSandbox` for `tandem send`, `recv`, `status`, `log` and `watch`, and the navigator profile
  carries `unsandboxed(tandem send)` and so on for each. A bare `unsandboxed(tandem)` would also
  have let a sandboxed navigator run `tandem claude -- --permission-mode bypassPermissions` (a
  host-side writer), `tandem init --replace` or `tandem done` with no prompt.
- New peers (Copilot, Codex) are added as new launchers plus a peer section in the protocol
  template; the bus, the CLI and the shared rules don't change.

### Implementation: Python 3.9+, stdlib only

The team runs macOS and Linux, so tandem is plain Python source that runs unchanged on both, with no
build step and no dependencies.

- **Layout: a `tandem/` package plus a `bin/tandem` shim** (`#!/usr/bin/env python3`, no `.py`
  extension) that resolves its own realpath and imports the package. Install is a clone plus a
  symlink of `bin/tandem` into `~/.local/bin` (`make install`). Agents type plain `tandem …`, which
  agy's `unsandboxed(tandem send)` and Claude's `Bash(tandem send *)` (one rule per protocol
  subcommand) match. Generated hooks call the shim by absolute path (so they don't depend on the
  harness's PATH) and are all tandem subcommands (`tandem hook <event>`, and `tandem watch --once`
  for Claude's waiter), so hook logic is tested Python rather than generated shell.
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
`--as` defaults to `TANDEM_ROLE` inside a launched harness. `tandem --version` prints the version.

Exit codes: 0 ok; 1 any error, usage errors included (argparse's usual 2 would read as mail to
Claude's waiter); 2 mail (`watch --once` only); 3 no mail or timeout (`recv`); 130 Ctrl-C in a
blocking command.

| Command | Purpose |
|---|---|
| `tandem init [name] [--ticket KEY] [--task TEXT \| --task-file F] [--test-cmd CMD]... [--replace]` | create the session tree and point the repo at it. `--test-cmd` (repeatable) replaces detection: `go.mod` → `go test`, `dip.yml` → `dip rspec`, a `Gemfile` without `dip.yml` → `bundle exec rspec` |
| `tandem send [--as R] --to R [--type T] [--reply-to ID] [--commit REV] [--file PATH]... [--expects-reply \| --no-reply] [body]` | body as the argument, else from stdin (refused on a terminal); tmp, mv to new, append to log. `expects_reply` defaults to true for `plan`, `review_request`, `question` and `escalate` |
| `tandem recv [--as R] [--wait] [--timeout S] [--peek] [--json]` | pop the oldest (lowest-id) `new/` message to `cur/` and print it; `--peek` doesn't consume; `--timeout` implies `--wait`; exit 3 and `no mail for <role>` on stderr when nothing arrives; a wait gives up once the session has ended |
| `tandem watch [--as R] (--once [--deadline S] \| --detach)` | wait for mail and ring the harness doorbell; driver and navigator only. `--once` (Claude's asyncRewake waiter): exit 2 with the doorbell on stderr when an unread message above `peers/<role>/rung` arrives (one ring per batch, for the highest id; mail left unread isn't rung again), exit 0 at `--deadline`, when the session ends, or when a newer waiter's pid takes over `peers/<role>/watch.pid` (`{"pid": …, "ls_address": null}`, removed on exit). `--detach` (agy, run by agy itself through `run_command`; refuses without `ANTIGRAVITY_LS_ADDRESS`/`ANTIGRAVITY_CSRF_TOKEN`): double-forks a watcher into its own session with stdio on `peers/<role>/watch.log`, prints its pid and returns at once. The watcher rings with `$ANTIGRAVITY_AGENTAPI_EXE agentapi send-message <id> <doorbell>`. The id is read on every ring from the one `presence/<conversation>.lock` the agy process holds open (`/proc/<pid>/fd` on Linux, `lsof -p` on macOS; the agy pid is the nearest `agy` above the `--detach` call, or `TANDEM_AGY_PID`), else from `peers/agy/conversation_id`, else `ANTIGRAVITY_CONVERSATION_ID`; a failed send (non-zero exit or an `{"error":…}` reply) isn't marked rung and is retried every 5 s. Pidfile `{pid, ls_address}`: a live watcher for the same LS address means "already running" (decided by the detached child under the session lock), any other pidfile is taken over. It exits when the LS port refuses a connect (checked every 5 s), when superseded, when the session ends or on SIGTERM, and logs `watcher_started`/`watcher_exited` events. `ANTIGRAVITY_LS_ADDRESS` is read as `host:port`, `[v6]:port` or a URL such as `http://host:port/`; agy 1.5.6 gives a `run_command` child `localhost:<port>`, and an address in none of those forms skips the LS check (logged once) instead of reading as a gone server |
| `tandem log [--json] [-f]` | print `log.jsonl` readably (`--json`: lines as stored); `-f` follows by byte offset and stops once the session has ended |
| `tandem status` | every active session (`--session` narrows to one, ended or not): pending counts per inbox, last message sent by each role, watcher liveness from `peers/<role>/watch.pid`, stale repo pointers |
| `tandem done` | write `summary.md`, mark the session ended, log `done` and clear the repo's active-session pointer; refuses an ended session |
| `tandem human` | REPL for the user, sending as `human`; refuses an ended session. Every 1 s it pops new `human` mail to `cur/` and prints it, then reads input lines (one line is one message). `@driver <text>` / `@navigator <text>` answer (`reply_to`) the latest message from that role the user has read (`inbox/human/cur/`), else send a `decision`; `@role:<type> <text>` sets the type (only `answer` threads); bare text goes to the driver; a mistyped `@role`, type or `/command` is refused, never sent. `/log` and `/status` print this session's `tandem log` / `tandem status` output, `/help`, `/quit`. Ctrl-D, Ctrl-C and `/quit` exit 0; when the session ends (`tandem done` elsewhere) it prints any last mail, says so and exits 0 |
| `tandem hook <event>` | internal, run by the generated harness configs: stdin JSON in, the hook's JSON (or nothing) out, always exit 0. A failure (malformed stdin, unknown session, any exception) prints the hook's safe output and adds one line to `peers/<peer>/hooks.log` (stderr when no session resolved). `claude-prompt` (UserPromptSubmit) appends `{"kind":"console","from":"human","to":<role claude holds in session.json>,"peer":"claude","body":<prompt>}` to `log.jsonl` and prints nothing, since its stdout would join the model's context; it skips blank prompts, prompts starting with `tandem:` (the launcher's start line), an asyncRewake wake (a prompt starting with `<task-notification>` that carries the doorbell, which is how Claude 2.1.322 delivers the Stop hook's wake to `UserPromptSubmit`; seen live in plan Task 17) and ended sessions. The agy hooks all write `conversationId` to `peers/agy/conversation_id` (tmp + rename) first. `agy-pretool` (PreToolUse, `run_command`) prints the bypass, `force_ask` or `ask` decision described under [Permissions → Decision](#decision), never `{}`. `agy-session-start` injects a system message naming agy's role and session, the exact watcher command when no live agy watcher holds `peers/<role>/watch.pid`, and the pending count (that mail is then marked rung). `agy-preinvocation` logs `lastUserInput` as a `console` line once: it skips blank input, the doorbell and `tandem:` lines, and input equal to `peers/agy/last_input`. The hook fires before every model call of a turn with the same input and the payload carries no known turn marker, so the same text typed twice in a row (`yes`, then `yes`) is logged once (plan Task 17 looks for a marker). `agy-stop`, only on `terminationReason` `NO_TOOL_CALL` with `fullyIdle`, returns `continue` to restart a dead watcher (at most once per 60 s, `peers/agy/watchdog`) and/or to read unrung mail (marked rung); otherwise `{}`. Ended sessions get the no-op output |

## Waking an agent: the doorbell rule

An LLM session only acts when its harness gives it a turn, so notification is the hard part, not
transport. **The nudge is a doorbell, not the mail.** Whatever wakes an agent carries a fixed line
(``tandem doorbell: new mail for <role>. Run `tandem recv`.``), never message content. Lost or
duplicate nudges are therefore harmless, and the bus stays the single source of truth.

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

Chosen design (implemented; live-checked on agy 1.5.6 in plan Task 17):

- agy starts `tandem watch --as <role> --detach` itself via `run_command`. `--detach` double-forks out
  of the tool call in place of the probes' `nohup … &`, so the command has no shell metacharacters.
  Under the fallback and driver profiles (no sandbox; the navigator's default from agy 1.5.6) the
  hook passes it through and `command(tandem watch)` approves it. Under the sandboxed navigator
  profile (1.5.4–1.5.5) the hook forces the bypass and `unsandboxed(tandem watch)` approves it.
- A `SessionStart` hook injects a system message naming agy's role, the exact watcher command when no
  live watcher holds the pidfile, and any pending mail, so the model doesn't have to remember it from
  the protocol.
- A `Stop` hook is the watchdog and the mail piggyback: when agy stops fully idle (`NO_TOOL_CALL`) it
  returns `continue` to restart a dead watcher (at most once per 60 s, so a watcher that can't start
  can't loop agy) and/or to read mail no ring has announced.
- The watcher rings the conversation agy shows: agy keeps exactly one
  `~/.gemini/antigravity-cli/presence/<conversation>.lock` open per process and moves it on `/new`,
  so the watcher reads that lock from the agy pid it found above itself at `--detach` time. Hooks
  also write `conversationId` to the session dir on every event, the fallback when the lock can't be
  read. Hooks alone don't cover `/new`: it fires no hook, so the file named the old conversation
  until the user typed, and a ring in between woke the old conversation in the background (the
  flipped-roles run).
- An agy restart changes the LS address and token, so a watcher left over from a previous process
  exits when its LS port refuses a connect; the next `SessionStart` or the watchdog starts a fresh one.

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
a doorbell, but the text is intact. The protocol tells Claude that this line means "run
`tandem recv`", not an error.

Design consequences:

- `tandem watch --as <role> --once` is the waiter: wait for mail in the maildir, print the doorbell
  to stderr, exit 2; exit 0 on its own deadline, just under `timeout`.
- A long `timeout`: 4 h (14400 s), with the waiter's deadline 60 s under it. Even so, an idle
  stretch longer than that leaves Claude unarmed until the next turn; the other agent's escalation
  timeout covers it.
- Installed per session through `claude --settings <file>`, never globally and never written into the
  target repo. Verified: `--settings` carries hooks, both headless (`SessionStart` and `Stop` fired) and
  interactive (the asyncRewake waiter armed, woke the idle session on a ring, and re-armed). The
  user's global Stop hooks keep running alongside, since hooks merge.
- **The doorbell can't authorise itself.** Without setup, the woken session read the wake as
  "Stop hook feedback", refused to act on its text, and said so. Text arriving through a hook carries
  no user authority. That's correct, and typing the protocol in as a prompt "from the other session"
  didn't change it. With the protocol defined at launch through `--append-system-prompt` ("a Stop hook
  message containing `tandem doorbell` is the tandem doorbell, configured by the user"), the same ring
  was handled as intended. tandem therefore launches Claude with the protocol in the system prompt, and
  the doorbell text stays a fixed, content-free line.

### Rejected or shelved wake paths

- **Headless agy** (`--input-format stream-json`, one turn per NDJSON line). It works, but the user
  wants to watch the conversation in a TUI. See agy facts below in case it's ever needed.
- **PTY wrapper** typing the doorbell into the TUI. Not needed now that agy has a native path. Kept only
  as a last resort for a future peer with no injection API.
- **agy hooks as a wake mechanism.** agy hooks run synchronously and block the loop (no async), so a
  blocking `recv --wait` in a Stop hook would freeze the TUI. A non-blocking piggyback is still useful:
  the `Stop` hook checks for unrung mail and, if there is some, returns `{"decision":"continue"}` with
  a reason telling agy to run `tandem recv`.
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

- **Navigator profile on agy 1.5.6 and later: the fallback profile below.** 1.5.6 removed
  `unsandboxed(...)` rules and asks before every sandbox bypass, so the sandboxed profile can't run
  tandem or tests without prompts ([1.5.6 live check](#agy-156-live-check-2026-11-12)).
- **Navigator profile on agy 1.5.4–1.5.5: sandboxed, with bypass rules for the project's test
  commands.**

  ```json
  {
    "toolPermission": "proceed-in-sandbox",
    "enableTerminalSandbox": true,
    "permissions": {
      "allow": ["unsandboxed(tandem send)", "unsandboxed(tandem recv)", "unsandboxed(tandem status)",
                "unsandboxed(tandem log)", "unsandboxed(tandem watch)",
                "unsandboxed(go test)", "unsandboxed(dip rspec)", "unsandboxed(bundle exec rspec)"],
      "deny": ["write_file(*)"]
    }
  }
  ```

  Commands run in the sandbox with no prompts (no network, nothing outside the workspace); tests
  and the protocol's tandem subcommands run on the host for real. The test prefixes are per
  project: tandem derives them from the repo (`go.mod` → `go test`, `dip.yml` → `dip rspec`, ...)
  or takes `--test-cmd` at `init`. Caveat: a test prefix takes any arguments, and some of them write
  on the host or run other programs there (`go test -exec 'sh -c …'`, `-o <path>`,
  `-coverprofile=<path>`), so the navigator's read-only rule holds for its test runs only as far as
  the driver's `git status` check (`docs/backlog/test-prefix-bypass-takes-any-arguments.md`).
- **The tandem `PreToolUse` hook** decides each `run_command` call, in this order:
  1. a test prefix or one of the role's tandem subcommands (`tandem send`, `recv`, `status`, `log`,
     `watch`, and `done` for a driver) as one plain command (no `;`, `&`, `|`, redirect, `$` or
     backtick outside quotes; quoted text is literal, `$` and backticks inside double quotes
     aren't), matched on the command text the way agy matches rule prefixes (so `go  test` or
     `'tandem' send` don't match, rather than force a bypass no rule approves) →
     `{"decision":"allow","overwrite":{"BypassSandbox":true}}` under the navigator profile, the only
     one with `unsandboxed` rules (the hook reads `enableTerminalSandbox` from
     `peers/agy/profile.json`). An `unsandboxed` rule only approves a bypass the agent asks for, and
     the hook alone still prompts, so both are required (verified on 1.5.4 and 1.5.5). Under the
     fallback and driver profiles, which have no sandbox and no `unsandboxed` rules, the same
     commands get `{"decision":"ask"}` and their `command(...)` rules approve them.
  2. **write gate**: a command that looks like a write (redirects `>`/`>>`, `tee`, `touch`, `rm`,
     `mv`, `cp`, `mkdir`, `ln`, `chmod`, `sed -i`, `perl -i`, `truncate`, `dd`, `git` subcommands
     that change the tree or refs such as `commit`, `checkout`, `reset`, `restore`, `stash`, `apply`,
     `merge`, `rebase`, `clean`, `add`, `rm`, `mv`, `config`, `remote`, `bisect`, `format-patch`),
     or a tandem subcommand outside the role's list (`tandem claude`, `agy`, `init`, `done`,
     `human`, `hook`, which would start a harness or end the session) → `force_ask`, a visible
     prompt the user answers.
     Needed because agy 1.5.5's sandbox lets shell commands write the workspace (see
     [the 1.5.5 live probe](#agy-155-live-probe-2026-11-11)). It's best effort, since writes through
     `python3 -c` or a script can't all be spotted; `git status` stays the net. It applies only
     while agy is the navigator; a driver's writes get `ask`. The command is read the way `sh` reads
     it (quoted text is literal) and every part is checked, including `$(…)`, backticks, subshells
     and each side of `;`/`&&`/`||`/`|`. `2>&1`, `>&2` and redirects to `/dev/null` aren't writes;
     `git push`, `pull` and `fetch` are gated too, and read-only forms (`git stash list`, `git tag`
     or `git branch` listing, `git config --get`, `git remote -v`) aren't. Wrappers are skipped with
     their option values (`sudo -u bob rm x` is `rm`). Rules live in `tandem/cmdline.py`.
  3. anything else → `{"decision":"ask"}`, the pass-through, which runs sandboxed with no prompt.
- **Fallback profile** (the navigator's default on agy 1.5.6 and later, `tandem agy --profile
  fallback`, or automatic when the self-check fails), for when `unsandboxed` rules stop working:
  no sandbox,
  `request-review`, a tight allowlist (read-only git: `git status|diff|log|show` and the same as
  `git -C <repo> …`, which agy uses unprompted; the protocol's tandem subcommands such as
  `command(tandem send)`, the exact test commands such as `command(dip rspec)`, never
  `command(dip)`), `write_file(*)` denied, and the hook gating shell writes.
  Before 1.5.6, tandem checks at startup that agy loads the navigator profile as written and
  switches to this profile if it doesn't; a version not yet verified live only warns (see the self-check under
  [Launchers](#launchers-tandem-peer-is-the-per-peer-adapter)). Every profile's `deny` also carries
  the user's global agy deny rules.
- **Driver profile (when agy drives)**: `--mode accept-edits`, `toolPermission: request-review`,
  allow the driver's tandem subcommands (the navigator's plus `command(tandem done)`), the test
  commands, `command(git)`, and `write_file(<session dir>/plan.md)` and
  `write_file(<session dir>/decisions.md)` (outside the workspace, so `accept-edits` alone asks),
  `deny` = the user's global agy deny (often empty), no sandbox
  (the driver needs network and the full toolchain; on 1.5.4 the sandbox also made the workspace
  read-only). Prompts are left only for irreversible actions, which the user wants to see anyway.
- **Safety net for any profile**: the driver runs `git status` at each checkpoint and treats any change
  it didn't make as a navigator violation. Tests legitimately write gitignored paths (`coverage/`,
  `log/`, `tmp/`), which `git status` already ignores.
- The doorbell never prompts: agy's `tandem watch` is started once through `run_command`, approved
  by `command(tandem watch)` (or, under the sandboxed 1.5.4–1.5.5 profile, with the bypass forced by
  the hook and approved by `unsandboxed(tandem watch)`), and each ring is an `agentapi` call from
  that watcher, not a tool call.
- Claude gets a per-session settings file too: `Bash(<prefix> *)` allowed for the same prefixes
  (the role's tandem subcommands and the test commands, so neither role prompts on a test run), and
  for a navigator `Edit`, `Write` and `NotebookEdit` denied. Its Bash isn't write-gated yet
  (`docs/backlog/claude-navigator-bash-can-write.md`); the driver's `git status` check covers it.

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

- **Navigator profile (sandbox)**: on 1.5.4 the sandbox made the workspace read-only, so shell
  writes failed. On 1.5.5 it doesn't, so the hook's write gate turns write-looking commands into a
  visible prompt. Only the bypassed prefixes (tests, the protocol's tandem subcommands) run outside
  the sandbox, which is why the hook matches exact prefixes and never `dip` alone.
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
and may disappear in a future release. The 1.5.5 notice says such rules are ignored, but the
[1.5.5 live probe](#agy-155-live-probe-2026-11-11) showed they still work.

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

## Protocol rules

- Phases: intake (ticket, `--task` brief, or the user's first prompt), plan, plan review, checkpointed implement + review, summary.
- **Bounded consensus.** The navigator must state risks and its strongest objection before it can
  approve. At most 3 rounds per item, then `escalate` to `human` with both positions. "Loop until agreed" is rejected because
  LLMs cave or loop.
- **Checkpointed review**, per plan step or commit, not continuous parallel review that chases a moving
  tree.
- Skip review for trivial changes.
- A peer agreeing is never user approval.
- A reply is never promised: a model can ignore a message that arrived fine.
- Summary is built from `decisions.md` plus `log.jsonl`.
- The shared rules never touch transport details. Only the per-peer section and launcher do, so
  peers can be swapped.
- The rules live in `tandem/templates/protocol.md`: shared rules plus one section per role and per
  peer. `tandem/protocol.py` renders them with the session's values for Claude's
  `--append-system-prompt` and agy's `.agents/rules/AGENTS.md`, so both peers get the same text.
  The task brief is cut at 6000 UTF-8 bytes in the rendered protocol, with a note pointing at the
  full text in `session.json`, so the protocol stays under 16 KB and agy's 24 KB rules cap holds
  for any brief, multibyte text included.
- A blocking wait is `tandem recv --timeout 100`: Claude's Bash tool kills a command at 120 s by
  default, which would cut the wait off before `recv` could exit 3 and trigger the escalation.
- Message bodies go in as one single-quoted argument, never a heredoc, pipe or redirect: the
  navigator's agy sandbox only bypasses plain commands, and both harnesses' permission rules match
  the command text.
- agy is told to run repo commands as `cd <repo> && <command>` (its shell starts in the session
  dir), otherwise in plain form (no `git -C <path>`, no absolute paths), and to read files with its
  file tools. On the 1.5.6 run the fallback
  navigator reached for `git -C /…/apix diff`, `make -C /…/apix help` and
  `go tool cover -func=/…/tmp/cover.out`, and each one asked the user.
- The driver ends a session only after the user confirms, and puts its narrative in `decisions.md`
  and its `done` message, because `tandem done` regenerates `summary.md`.

## agy facts (1.5.4, re-checked on 1.5.5 and 1.5.6)

The behaviour above (sandbox, bypass, hooks, `agentapi`) was verified on agy 1.5.4. On 1.5.5 the
config was re-run in print mode ([re-check](#agy-155-re-check-2026-11-11)) and one live session
probed the sandbox, the bypass and `--prompt-interactive` ([live probe](#agy-155-live-probe-2026-11-11)).
Hooks in a live 1.5.5 session and `agentapi` on 1.5.5 wait for plan Task 17. The 1.5.5 changelog
doesn't mention the sandbox, `unsandboxed` rules or `agentapi`.

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

  The non-`PreToolUse` rows come from the docs, not from a live run; plan Task 17 exercises them.
- **Initial prompt in interactive mode**: `-i` / `--prompt-interactive <prompt>` "runs an initial
  prompt interactively and continues the session" (`agy --help`, 1.5.5). There is no positional
  prompt. Launchers use `--prompt-interactive=<text>` (the `=` form, as with `-p=`); the 1.5.5 live
  probe confirmed it starts the first turn in the TUI.
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
- **An empty `deny: []` replaces the global deny; an omitted `deny` keeps it** (`-p=/config`: the
  global `command(rm -rf)` stays with no key and disappears with `[]`). Since any written `deny`
  replaces the global one, every profile writes tandem's deny rules plus the user's global ones
  (Task 15; Task 14 had written `deny: []` for the driver, which dropped them).
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
  to become the default. **Settled by the [live probe](#agy-155-live-probe-2026-11-11): the rules
  still work.**
- The model-facing sandbox description in the 1.5.5 binary says standard sandbox mode "has full
  read/write access to your workspace", while on 1.5.4 a sandboxed `touch` in the workspace was
  blocked. **Confirmed by the [live probe](#agy-155-live-probe-2026-11-11): the workspace is
  writable**, hence the hook's write gate.

### agy 1.5.5 live probe (2026-11-11)

One interactive agy 1.5.5 session in a throwaway git workspace, launched with
`--prompt-interactive=<text>` and a profile of `proceed-in-sandbox`, sandbox on,
allow `unsandboxed(python3 loop.py)`, deny `write_file(*)`. `loop.py` binds a loopback socket and
connects to it. `BypassSandbox` values were read from the conversation's transcript.

| # | Command | Bypass requested | Result |
|---|---|---|---|
| 1 | `touch p1.txt` (in the workspace) | no | **ran, file created**: the workspace is writable in the 1.5.5 sandbox |
| 2 | `python3 loop.py` | yes | ran outside, **no prompt**: `LOOPBACK-OK` |
| 3 | `python3 loop.py 'a; b'` | yes | ran outside, no prompt: the quoted `;` wasn't split into a second command |
| 4 | `python3 loop.py` | no | failed: loopback is still blocked in the sandbox |
| 5 | `python3 -c "print(42)"` (no rule covers it) | yes | **bypass prompt** (declined) |
| 6 | `touch ~/code/tmp/outside155.txt` | no | blocked: `Operation not permitted` |

Conclusions:

- **`unsandboxed(<prefix>)` still works on 1.5.5**, selectively: rows 2–3 pass silently, row 5
  prompts. The binary's "ignored and grant nothing" notice must apply to malformed rules only.
- **The sandbox no longer makes the workspace read-only** (row 1). Network, loopback and paths
  outside the workspace are still blocked (rows 4, 6). The navigator's read-only rule now rests on
  `write_file(*)`, the hook's write gate, and the driver's `git status` check.
- Quoted message bodies passed to a bypassed `tandem send` are safe from agy's command splitting
  (row 3).
- `--prompt-interactive=<text>` starts the first turn in the TUI, so launchers can use it.
- Declining a prompt interrupts the whole turn, so later tool calls in it don't run.

### agy 1.5.6 live check (2026-11-12)

The first plan Task 17 session, on agy 1.5.6, in a throwaway clone of apix with `tandem agy` on the
then-default sandboxed navigator profile. The self-check passed (`check=ok`), with the expected
"unverified on 1.5.6" note.

- **`unsandboxed(...)` rules are gone.** The TUI opened with `Invalid "unsandboxed" permission
  rules found; they are ignored and grant nothing:`, listing the profile's six rules (it named
  `~/.gemini/antigravity-cli/settings.json` as their file although they came from `--settings`). The
  binary says `unsandboxed is no longer a valid rule, use command() instead`, and its tool docs say
  `BypassSandbox: true` "Requires manual user approval".
- **Every hook-forced bypass prompted**: `tandem watch --as navigator --detach` and `tandem recv`
  both asked "can I run this command". So on 1.5.6 the sandboxed profile prompts on each tandem
  command and test run, and tandem now gives the navigator the fallback profile from 1.5.6 on.
- The self-check can't see this: `/config` still echoes the rules, and print mode shows no warning.
- `ANTIGRAVITY_LS_ADDRESS` in a `run_command` child is `localhost:<port>` (`localhost:64249`), the
  bare `host:port` form the watcher already parses.
- The detached watcher started and wrote `{pid, ls_address}`; `tandem recv` exited 3 (no mail),
  which agy's TUI shows as "failed".
- The `PreInvocation` hook had already recorded the initial `tandem:` prompt and the conversation
  id while the workspace trust prompt was still on screen.

The rest of that run, on the fallback profile, and a second session with roles flipped (agy driver,
Claude navigator) are recorded in plan Task 17. What changed tandem:

- An asyncRewake wake reaches Claude's `UserPromptSubmit` as a `<task-notification>` block carrying
  the doorbell and was logged as human input; the prompt hook now skips it.
- The fallback navigator ran `git -C <abs repo> diff`, `make -C … help` and
  `go tool cover -func=<abs path>`, each off the allowlist and so each a prompt. agy's commands start
  in the session dir (`pwd`), which is why it reached for `-C`. The profile now allows
  `git -C <repo> status|diff|log|show`, and the protocol has agy run repo commands as
  `cd <repo> && <command>`.
- `--mode auto` for a driver logged `auto mode is unavailable, falling back to default`: auto needs
  the terminal sandbox. Edits then asked, `plan.md` and `decisions.md` included (outside the
  workspace). Drivers now launch with `accept-edits` plus `write_file(...)` for those two files.
- After `/new` the watcher rang the old conversation (hooks hadn't fired yet), which handled the mail
  out of sight. agy holds `presence/<conversation>.lock` open for the conversation it shows, so the
  watcher now reads that.

### Claude Code permission rules (docs, 2026-11-11)

From code.claude.com/docs/en/permissions and /settings:

- **Prefix rule: `Bash(tandem *)`.** A trailing ` *` also matches the bare command (`tandem`), and
  the space is part of the rule, so `tandemx` doesn't match. `Bash(tandem:*)` is an equivalent older
  spelling; the permission dialog itself writes the space form, so tandem does too, one rule per
  protocol subcommand (`Bash(tandem send *)`, …) rather than `Bash(tandem *)`.
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

None blocking v0. What's left is live-only or out of scope:

1. ~~Whether agy's `lastUserInput` on `PreInvocation` distinguishes a human prompt from a
   `send-message` doorbell.~~ Made moot in Task 13: `agy-preinvocation` skips any input that starts
   with `tandem doorbell` or `tandem:`, so a doorbell is never logged as human input whichever way agy
   reports it. Whether a ring shows up in `lastUserInput` at all is still worth a look in Task 17.
2. Whether `plan` mode blocks running tests and shell writes. Worth a look now that the 1.5.5
   sandbox no longer keeps the workspace read-only.
3. ~~Re-check the navigator profile on agy 1.5.5 without a model turn~~: done, see
   [agy 1.5.5 re-check](#agy-155-re-check-2026-11-11). The profile loads, but print mode can't show
   whether `unsandboxed(...)` still works, which became item 4.
4. ~~Does agy 1.5.5 still honour `unsandboxed(...)`?~~ Yes, see
   [the 1.5.5 live probe](#agy-155-live-probe-2026-11-11).
5. ~~Is the workspace still read-only inside the 1.5.5 sandbox?~~ No. The navigator profile gains
   the hook's write gate (`force_ask` for write-looking commands). Whether `force_ask` shows a prompt
   under `proceed-in-sandbox` is still open: plan Task 17.
6. ~~What the `tandem agy` self-check can test.~~ Settled in Task 15: `/config` proves the profile
   parses and its rules survive; the version decides the rest. `VERIFIED_AGY_VERSIONS` (1.5.4,
   1.5.5) holds the versions where a live session confirmed the bypass; any other version before
   1.5.6 warns and keeps the navigator profile. Add a version there after re-running the live probe
   on it. From 1.5.6 the check doesn't run: those versions have no `unsandboxed` rules.
7. Live checks (plan Task 17). Settled on agy 1.5.6 and Claude 2.1.322: asyncRewake wakes a Claude
   launched by `tandem claude`; agy as navigator (fallback) and as driver starts its watcher and
   runs tandem and test commands with no prompt; a real agy restart; `tandem human` and an
   escalation; `ANTIGRAVITY_LS_ADDRESS` is `localhost:<port>`; `accept-edits` and the
   session-file `write_file` rules for a driver; after `/new` a ring reaches the new conversation
   (which acts on it once it has had a turn: type something first); the fallback navigator's
   `tandem done` gets the gate's prompt; agy's commands run in the session dir whatever tandem
   passes (hence `cd <repo> &&`). `cd <repo> && go test ./...` runs with no prompt. Still open: whether
   `PreInvocation` carries a per-turn marker, and whether agy hooks see `ANTIGRAVITY_LS_ADDRESS`.
