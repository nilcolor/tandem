# tandem design

The decision record: what tandem does, why, and what was rejected. The evidence behind each harness
fact lives in [`docs/probes/`](probes/README.md), one dated file per probe. `README.md` is for
people using tandem; `AGENTS.md` is for working on it. When a decision moves, update this file in the
same change.

## Requirements (from the user)

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
  for Edit, Write and NotebookEdit (its Bash isn't gated yet, see
  `docs/backlog/claude-navigator-bash-can-write.md`). agy as navigator: a `write_file(*)` deny plus
  the tandem `PreToolUse` write gate on shell commands, since the navigator runs without agy's
  sandbox ([Permissions](#permissions-autonomy-without-prompt-fatigue)).
- The navigator reviews evidence (diffs, commits, test runs it ran itself), not chat claims.

## Transport: maildir per session, no daemon

Sessions live in a **global spool**, `~/.tandem/sessions/<session>/`, not in the target repo. That
keeps the bus out of the repo's git state, and both launchers attach it to the harness's workspace
with `--add-dir`. `TANDEM_HOME` moves the whole spool (default `~/.tandem`). tandem creates the
spool's directories owner-only (0700): the log holds every prompt typed into a console.

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
- Ack: move from `new/` to `cur/`. Crash recovery is free. An unreadable message is moved to `cur/`
  and reported, so its id is never reused and the inbox isn't blocked.
- Rejected: a unix socket broker (a daemon to manage, with nothing gained over a directory); the
  agterm mailbox (not terminal agnostic); pasting content into prompts (the `peer-chat` approach).

### Naming

The session name is only a label for addressing; the ticket is separate, optional input for intake.

- Default name: the branch's last segment (`feature/login-timeout` → `login-timeout`); `--ticket`
  gives the ticket key; on detached HEAD, `main` or `master`, `<repo>-<MMDD-HHMM>` (local time).
- Names are sanitized to `[a-z0-9-]` on disk (NFKD first, so `Café` becomes `cafe`; capped at 48
  characters); the original string is kept in `session.json`. An explicit name or ticket with no
  `[a-z0-9]` is an error.
- The spool is global, so the directory is prefixed with the repo, `apix--fix-csv-bom`, while
  the user keeps typing `fix-csv-bom` (resolution is per repo). A collision in the same repo appends
  `-2`, and the directory is claimed with `mkdir`, so two concurrent inits can't share one.
- **Task source for intake**, whichever exists: the linked ticket, `--task` / `--task-file` given to
  `init`, or the user's first prompt in the driver's console (captured by the human-input hook).

### Finding the session from any pane

An env var doesn't cross panes or windows, so the session is keyed to **the repo**. `TANDEM_SESSION`
is only how a launcher hands the session to the harness it starts (and from there to its hooks and to
`tandem send/recv`).

- `tandem init` in a repo creates the session, records the repo path in `session.json`, and points
  the repo's active-session pointer at it: `~/.tandem/repos/<sha256 of git toplevel, 16 hex>/current`.
- Launchers run in **any** pane inside that repo resolve the pointer, export `TANDEM_SESSION` and
  `TANDEM_ROLE` into the harness, and `exec` it.

Resolution order, first match wins:

| # | Source | Used by |
|---|---|---|
| 1 | `--session <name>` | explicit override, from anywhere |
| 2 | `TANDEM_SESSION` | `tandem send/recv/watch` typed by an agent inside a launched harness (generated hooks pass `--session <dir>`, step 1) |
| 3 | the active session for the current repo (`git rev-parse --show-toplevel`, so subdirectories work) | launchers, the normal case |
| 4 | the only active session on the machine, outside a git repo only | `tandem human` outside any repo |
| 5 | otherwise: error listing the active sessions | ambiguity is never guessed |

- **Worktrees** have different toplevels, so parallel tasks in worktrees get separate sessions.
- Step 4 applies only outside a git repo: inside a repo or worktree without its own session, taking
  the machine's only session would attach a launcher to another tree's plan.
- `TANDEM_SESSION` naming a missing session is an error, not a fall-through, so a launched harness
  can't silently attach to a different session.
- Steps 1 and 2 return an ended session too; each command decides whether that is an error (hooks and
  watchers exit quietly on one). A `--session` short name matches the current repo's sessions first,
  then all sessions; among several matches, the single active one wins.
- Anything waiting on a session (`recv --wait`, `log -f`, both watchers) stops when it ends, is
  removed, or its `session.json` stops parsing: `send` refuses such a session, so no more mail can
  arrive. `session.json` is replaced atomically, so a reader never sees it half-written.
- **One active session per checkout.** `tandem init` refuses while one is active unless `--replace`:
  one working tree can't take two drivers anyway. `--replace` ends the old session without a
  summary (its `session.json` gets `ended` and `replaced_by`, its log a `replaced` event), under that
  session's lock. A pointer to an ended, missing or unreadable session is stale and never blocks
  `init`.
- **Ending.** `tandem done` writes the summary, marks the session ended and clears the repo pointer,
  all under the session lock that `send` takes, so no message can land after the summary is
  written. After `done` the repo resolves to no session, so an ended session's `log`/`status` need
  `--session`.
- `summary.md` is mechanical: session facts, task, a one-line-per-message timeline, every console input
  in full, the bodies of `done` messages, then `decisions.md` verbatim. The narrative is the driver's,
  in `decisions.md` or its `done` message, written before it calls `tandem done`.
- `init` stays explicit; launchers don't auto-init.

### The human

The user takes part two ways, both first-class:

- **Typing into either agent's console** (usually the driver's). This bypasses the bus, so each
  launcher installs a hook that appends every prompt the user types to `log.jsonl` as `console` from
  `human`: `UserPromptSubmit` for Claude, `PreInvocation`'s `lastUserInput` for agy. Decisions made
  in a console then still reach the summary.
- **`tandem human`** from any terminal, even outside the repo (resolution step 4): reads the `human`
  inbox and sends to either role. A message without a type answers the last message the user read
  from that role, so a reply to an escalation threads without typing an id.

No separate notifier: an agent that escalates also says so in its own console, so the user sees it
where they are already looking; the protocol requires it.

### Envelope

```json
{"id":"0007","from":"driver","to":"navigator","type":"review_request",
 "reply_to":"0005","expects_reply":true,
 "refs":{"commit":"abc123def456","files":["plan.md"]},
 "body":"markdown..."}
```

- Types: `plan`, `review_request`, `review`, `question`, `answer`, `decision`, `escalate`, `done`.
  `expects_reply` defaults to true for `plan`, `review_request`, `question` and `escalate`.
- `expects_reply` plus a `recv --timeout` after which the waiting agent escalates to `human` prevents
  mutual-wait deadlock. `recv` itself never escalates: on timeout it exits 3, and the protocol tells
  the waiting agent to escalate, so the escalation carries the agent's own account.
- Ids are per session, shared by all three inboxes, gap-free: the next id is one above the highest
  message in any `new/` or `cur/`, allocated under the session's `flock`, so `reply_to` names one
  message whichever inbox it sits in. `--reply-to` must name an existing message.
- `--type` defaults to `answer` with `--reply-to`, else `question`. A reply that defaulted to
  `question` would expect a reply back, and two agents would answer each other's answers forever.
- `--commit` is resolved in the session's repo and stored as a 12-char sha, so `HEAD` keeps meaning
  the commit it named when the message was sent. `--file` paths are stored as given.

## Launchers: `tandem <peer>` is the per-peer adapter

Each peer needs a specific launch: a permission profile, hooks, the protocol in its instructions,
session env, and for agy a profile choice. The launcher sets all of that up and then `exec`s the real
binary, so the harness owns the terminal directly (no wrapper process, no PTY). tandem never opens
panes: the user runs each launcher in whatever terminal they like.

- `tandem claude`: `exec claude --add-dir <session-dir> --settings <file> --append-system-prompt
  <protocol> [extra] -- <tandem: prompt>`. The settings carry the Stop hook (`asyncRewake`), the
  `UserPromptSubmit` human-input log and the permissions. The second `--` keeps a variadic flag in
  `extra` (`--allowed-tools`) from swallowing the initial prompt.
- `tandem agy`: `exec agy --settings <profile> --add-dir <session-dir> [--mode accept-edits]
  --prompt-interactive=<tandem: prompt> [extra]`. `--prompt-interactive` gives agy a first turn in
  the TUI, so `SessionStart` fires and tells it to start its watcher; `agy-preinvocation` skips the
  line.
- Defaults follow the user's preference: Claude drives, agy navigates. `--role` flips them, and the
  launcher picks the matching profile.
- **Role claims.** A launcher records `claims.<peer> = {role, pid, ts}` in `session.json` and logs a
  `role_claimed` event before it `exec`s. `exec` keeps the pid, so a claim lives exactly as long as
  the harness. `--role` taking the other peer's role swaps the two in `roles`, unless that peer has a
  live claim (then it's an error: quit it first). A dead claim is released, so relaunching after a
  crash never deadlocks. A second launcher for the same peer isn't refused yet
  (`docs/backlog/second-launcher-for-same-peer-not-refused.md`). A pair is one Claude and one agy:
  claims and peer dirs are keyed by peer, so two of the same harness collide
  (`docs/backlog/same-harness-pair.md`).
- Launchers export `TANDEM_SESSION` and `TANDEM_ROLE`, so hooks and `tandem send/recv` know who they
  are without flags, and `cd` to the repo's toplevel before `exec`.
- **Protocol parity.** Both peers get the doorbell protocol and role rules from launch-time,
  user-authored instructions rendered from one template: Claude through `--append-system-prompt`
  (copy in `peers/claude/protocol.md`), agy through `.agents/rules/AGENTS.md` in the session dir,
  loaded with `--add-dir` ([customizations from an `--add-dir`](probes/2026-11-10-agy-1.5.4-permissions-and-sandbox.md#customizations-from-an---add-dir)).
  No tandem skill or agy plugin ships: launch-time instructions already reach both peers with user
  authority, and a skill would be one more thing to install.
- **What `tandem agy` writes** (`tandem/launch_agy.py`): `.agents/hooks.json` with one hook name,
  `tandem`, holding `PreToolUse` (a `run_command` matcher group), `SessionStart`, `PreInvocation` and
  `Stop`, each calling `'<shim>' --session <dir> hook agy-…` (event names at the top level load
  nothing, [1.5.5 re-check](probes/2026-11-11-agy-1.5.5-recheck.md)); `.agents/rules/AGENTS.md`, the
  rendered protocol for agy's role (refused if over agy's 24 KB rules cap); and
  `peers/agy/profile.json`, the navigator or driver profile ([Permissions](#permissions-autonomy-without-prompt-fatigue)).
  Nothing is installed globally and nothing is written into the target repo.
- The test prefixes come from `session.json` `test_cmds` (detected at `init` or given with
  `--test-cmd`; whitespace collapsed and duplicates dropped). One containing `(` or `)` is left out
  of every rule set, since it would end a rule early.
- **Each agy profile's `deny` carries the user's own.** A `deny` in `--settings`, even `[]`, replaces
  the global one, so tandem writes its own deny rules followed by the user's global agy deny
  (`~/.gemini/antigravity-cli/settings.json` `permissions.deny`, read and never written,
  deduplicated). The user's global `allow` is not carried over: the profile is the whole allowlist.
- **agy 1.5.6 or later only.** `tandem agy` runs `agy --version` before claiming a role. A
  parseable version below 1.5.6 (`MIN_AGY_VERSION`) is refused with a pointer to `agy update`, and
  nothing is written; a version string tandem can't read only warns, so a format change in a later
  agy doesn't lock users out. 1.5.6 is the first version the profiles, hooks and watcher were
  checked live on; 1.5.4 and 1.5.5 sandboxed commands by default and needed `unsandboxed(...)`
  bypass rules, which 1.5.6 removed ([2026-11-12 live check](probes/2026-11-12-agy-1.5.6-claude-2.1.322-live-check.md)).
  Supporting both would mean a second navigator profile, a forced-bypass branch in the hook and a
  startup self-check that couldn't tell the versions apart anyway (`/config` echoes rules it
  ignores). The launch logs an `agy_profile` event with the role and the version.
- **agy's commands run in the session dir.** Each `run_command` gets a new shell in agy's first
  workspace root, which is the `--add-dir` session dir whatever order the dirs are given in, and
  agy ignores a hook-set `Cwd` ([2026-11-12 live check](probes/2026-11-12-agy-1.5.6-claude-2.1.322-live-check.md)).
  So the protocol has agy run repo commands as `cd <repo> && <command>`, which agy's rules approve
  part by part (`cd` is on agy's own system allowlist) and the write gate still checks; tandem
  commands stay plain, since they work from any directory.
- New peers (Copilot, Codex) are added as new launchers plus a peer section in the protocol
  template; the bus, the CLI and the shared rules don't change.

## Waking an agent: the doorbell rule

An LLM session only acts when its harness gives it a turn, so notification is the hard part, not
transport. **The nudge is a doorbell, not the mail.** Whatever wakes an agent carries a fixed line
(``tandem doorbell: new mail for <role>. Run `tandem recv`.``), never message content. Lost or
duplicate nudges are therefore harmless, and the bus stays the single source of truth. Each message
is rung once (`peers/<role>/rung` holds the highest id rung). Wake paths are per harness; the
protocol is shared.

### agy: `agentapi send-message` from a detached watcher

`agy agentapi send-message <conversation_id> <text>` starts a turn in a running TUI, queues behind a
busy one, and needs agy's environment, which only `run_command` children get
([agentapi probe](probes/2026-11-10-agy-1.5.4-agentapi-wake.md)).

- agy starts `tandem watch --as <role> --detach` itself via `run_command`. `--detach` double-forks
  out of the tool call (no `nohup … &`, so the command has no shell metacharacters), reports the
  watcher's pid once it has claimed `peers/<role>/watch.pid` (`{pid, ls_address}`), and returns. A
  live watcher for the same LS address means "already running", decided by the detached child under
  the session lock; any other pidfile is taken over.
- The watcher rings with `$ANTIGRAVITY_AGENTAPI_EXE agentapi send-message <id> <doorbell>`. The id is
  read on every ring from the one `presence/<conversation>.lock` the agy process holds open
  (`/proc/<pid>/fd` on Linux, `lsof -p` on macOS; the agy pid is the nearest `agy` above the
  `--detach` call, or `TANDEM_AGY_PID`), else from `peers/agy/conversation_id` (which every agy hook
  rewrites), else `ANTIGRAVITY_CONVERSATION_ID`. The lock comes first because `/new` fires no hook,
  so the hooks' file names the old conversation until the user next types, and a ring to it wakes
  the old conversation out of sight. A fresh conversation acts on a ring only after its first turn.
- A failed send (non-zero exit or an `{"error":…}` reply) isn't marked rung and is retried every
  5 s.
- It exits when the LS port refuses a connect (checked every 5 s), when superseded, when the session
  ends or on SIGTERM, and logs `watcher_started`/`watcher_exited` events. `ANTIGRAVITY_LS_ADDRESS` is
  read as `host:port` (what 1.5.6 gives), `[v6]:port` or a URL; an address in none of those forms
  skips the LS check (logged once) instead of reading as a gone server.
- A `SessionStart` hook injects a system message naming agy's role, the exact watcher command when no
  live watcher holds the pidfile, and any pending mail (then marked rung).
- A `Stop` hook is the watchdog and the mail piggyback: when agy stops fully idle (`NO_TOOL_CALL`) it
  returns `continue` to restart a dead watcher (at most once per 60 s, so a watcher that can't start
  can't loop agy) and/or to read mail no ring has announced.
- An agy restart changes the LS address and token, so a leftover watcher exits when its LS port
  refuses a connect; the next `SessionStart` or the watchdog starts a fresh one.
- Rejected: copying the LS address and token into a file to call from outside (a live credential on
  disk); a sidecar (the CLI doesn't run them).

### Claude Code: Stop hook with `asyncRewake`

An `asyncRewake` Stop hook that exits 2 wakes an idle session with its stderr, and `timeout` is
honoured past the 600 s default ([asyncRewake probe](probes/2026-11-10-claude-asyncrewake.md)).

- `tandem watch --as <role> --once` is the waiter: it claims `peers/<role>/watch.pid`, waits for an
  unread message above `rung`, prints the doorbell to stderr and exits 2; exit 0 on its deadline,
  when the session ends or when a newer waiter takes the pidfile.
- Timeout 4 h (14400 s), with the waiter's deadline 60 s under it. An idle stretch longer than that
  leaves Claude unarmed until the next turn; the other agent's escalation timeout covers it.
- Installed per session through `claude --settings <file>`, never globally and never in the target
  repo. The user's global Stop hooks keep running alongside, since hooks merge.
- **The doorbell can't authorise itself.** Text arriving through a hook carries no user authority,
  so tandem defines the doorbell in the system prompt at launch (`--append-system-prompt`), and the
  doorbell stays a fixed, content-free line. The wake arrives as `Stop hook blocking error from
  command "Stop": <doorbell>`; the protocol says that isn't an error.
- On 2.1.322 the wake also reaches `UserPromptSubmit` as a `<task-notification>` block carrying the
  doorbell; the human-input hook skips it.

### Rejected or shelved wake paths

- **Headless agy** (`--input-format stream-json`). It works, but the user wants to watch agy in a TUI.
- **PTY wrapper** typing the doorbell into the TUI. Not needed now that agy has a native path; a last
  resort for a future peer with no injection API.
- **agy hooks as a wake mechanism.** agy hooks run synchronously and block the loop, so a blocking
  wait in a hook would freeze the TUI. The non-blocking `Stop` piggyback above is what's left.
- **ACP server** (`agy acp`). The most capable option, but it needs a JSON-RPC client and a non-TUI
  front end.

## Permissions: autonomy without prompt fatigue

agy asks for confirmation on anything outside its allowlist, which stalls autonomous work. tandem
launches each agent with a **per-role permission profile** passed at launch and never saved, so the
user's own config stays untouched.

### agy levers

| Lever | Values | Effect |
|---|---|---|
| `--mode` / `agentMode` | `default`, `accept-edits`, `plan`, `auto` | `auto` approves edits and safe commands and sends commands leaving the sandbox to a reviewer agent; it needs the terminal sandbox |
| `toolPermission` | `always-proceed`, `request-review` (user's default), `strict`, `proceed-in-sandbox` | whether terminal commands need approval |
| `enableTerminalSandbox` | bool (user's default `false`) | restricted environment for commands |
| `permissions.allow` / `deny` | `command(prefix)`, `read_file(path)`, `write_file(path)`, `execute_url(domain)`, `mcp(server/tool)` | `command(...)` is a prefix match on the command text, and compound commands are split and checked part by part |
| `PreToolUse` hook | `allow`, `deny`, `ask`, `force_ask` per call | programmable policy over each tool call's JSON |
| `--settings <json\|file>` | any settings keys | per session, never saved: the carrier for tandem's profiles; replaces `allow` and `deny` separately |
| `--dangerously-skip-permissions` | none | rejected: too blunt, especially for a driver |

### agy profiles

Both keep agy's sandbox off and terminal commands under `request-review`, whatever the user's global
settings say, so the allowlist decides what runs unasked.

- **Navigator**: a tight allowlist: the protocol's tandem subcommands (`command(tandem send)` and so
  on), the exact test commands (`command(dip rspec)`, never `command(dip)`), and read-only git
  (`git status|diff|log|show`, and the same as `git -C <repo> …`, which agy reaches for on its own);
  `write_file(*)` denied, and the hook gating shell writes. Anything else prompts.
- **Driver**: `--mode accept-edits` (`auto` would need the sandbox and silently fell back to the
  default mode), the driver's tandem subcommands (the navigator's plus `tandem done`), the test
  commands, `command(git)`, and `write_file(<session dir>/plan.md)` and
  `write_file(<session dir>/decisions.md)` (outside the workspace, so `accept-edits` alone asks);
  `deny` = the user's global deny. Prompts are left for commands off the allowlist.
- An allowed test prefix takes any arguments, and some of them write on the host or run other
  programs (`go test -exec 'sh -c …'`, `-o <path>`), so the navigator's read-only rule holds for its
  test runs only as far as the driver's `git status` check
  (`docs/backlog/allowlisted-test-prefix-takes-any-arguments.md`).
- **Safety net for any profile**: the driver runs `git status` at each checkpoint and treats any
  change it didn't make as a navigator violation. Tests legitimately write gitignored paths
  (`coverage/`, `log/`, `tmp/`), which `git status` already ignores.

### The `PreToolUse` hook

`agy-pretool` decides each `run_command` call:

- **Write gate**, while agy isn't the driver: a command that looks like a write (redirects
  `>`/`>>`, `tee`, `touch`, `rm`, `mv`, `cp`, `mkdir`, `ln`, `chmod`, `sed -i`, `perl -i`,
  `truncate`, `dd`, `git` subcommands that change the tree or refs such as `commit`, `checkout`,
  `reset`, `restore`, `stash`, `apply`, `merge`, `rebase`, `clean`, `add`, `push`, `config`,
  `remote`), rubocop's autocorrect and file-writing options (`-a`, `-A`, `-x`, `-o`,
  `--auto-gen-config`, run directly or through `dip`/`bundle exec`, since `dip rubocop` is an
  allowed test command), or a tandem subcommand outside the protocol's list (`claude`, `agy`, `init`, `done`,
  `human`, `hook`) → `force_ask` with the reason `tandem write gate: …`. The command is read the
  way `sh` reads it, so a quoted message body is one literal argument and the words in it don't
  trip the gate, and every part is checked, including `$(…)`, backticks, subshells and each side of
  `;`/`&&`/`||`/`|`. `2>&1` and redirects to `/dev/null` aren't writes, read-only forms
  (`git stash list`, `git config --get`, `git remote -v`) aren't gated, and wrappers are skipped
  with their option values (`sudo -u bob rm x` is `rm`). It's best effort: writes through
  `python3 -c` or a script can't all be spotted (`docs/backlog/agy-write-gate-misses-indirect-writes.md`),
  so `git status` stays the net. Rules live in `tandem/cmdline.py`.
- Anything else → `{"decision":"ask"}`, the pass-through to agy's own rules. `{}` would deny.

### Claude

A per-session settings file: `Bash(<prefix> *)` allowed for the role's tandem subcommands and the
test commands, and for a navigator `Edit`, `Write` and `NotebookEdit` denied (a bare tool name in
`deny` removes the tool). Claude merges lists across scopes, so the user's own allow rules still
apply ([permission rules](probes/2026-11-11-claude-permission-rules.md)). Its Bash isn't write-gated
yet (`docs/backlog/claude-navigator-bash-can-write.md`); the driver's `git status` check covers it.

## Protocol rules

- Phases: intake (ticket, `--task` brief, or the user's first prompt), plan, plan review,
  checkpointed implement + review, summary.
- **Bounded consensus.** The navigator must state risks and its strongest objection before it can
  approve. At most 3 rounds per item, then `escalate` to `human` with both positions. "Loop until
  agreed" is rejected because LLMs cave or loop.
- **Checkpointed review**, per plan step or commit, not continuous parallel review that chases a
  moving tree. Trivial changes skip review and are listed in the next `review_request`.
- A peer agreeing is never user approval. A reply is never promised: a model can ignore a message
  that arrived fine.
- The shared rules never touch transport details. Only the per-peer section and launcher do, so
  peers can be swapped.
- The rules live in `tandem/templates/protocol.md`: shared rules plus one section per role and per
  peer, rendered by `tandem/protocol.py` with the session's values (repo, session dir, doorbell,
  test commands, message types from `maildir`). The task brief is cut at 6000 UTF-8 bytes in the
  rendered protocol, with a note pointing at the full text in `session.json`, so the protocol stays
  under 16 KB and agy's 24 KB rules cap holds.
- A blocking wait is `tandem recv --timeout 100`: Claude's Bash tool kills a command at 120 s by
  default, which would cut the wait off before `recv` could exit 3 and trigger the escalation.
- Message bodies go in as one single-quoted argument, never a heredoc, pipe or redirect: both
  harnesses' permission rules match the command text, and every part of a compound command needs
  its own rule.
- agy runs repo commands as `cd <repo> && <command>`, otherwise in plain form (no `git -C`, no
  absolute paths), and reads files with its file tools: each off-allowlist form is a prompt.
- The driver ends a session only after the user confirms, and puts its narrative in `decisions.md`
  and its `done` message, because `tandem done` regenerates `summary.md`.

## Hooks, in detail

`tandem hook <event>` is run by the generated harness configs: stdin JSON in, the hook's JSON (or
nothing) out, always exit 0. A failure (malformed stdin, unknown session, any exception) prints the
hook's safe output and adds one line to `peers/<peer>/hooks.log` (stderr when no session resolved).
Hooks call the shim by absolute path, so they don't depend on the harness's PATH.

- `claude-prompt` (UserPromptSubmit) appends a `console` record to `log.jsonl` and prints nothing,
  since its stdout would join the model's context. It skips blank prompts, prompts starting with
  `tandem:` (the launcher's start line), an asyncRewake wake (a `<task-notification>` carrying the
  doorbell) and ended sessions.
- The agy hooks all write `conversationId` to `peers/agy/conversation_id` first.
- `agy-pretool`: [the write gate above](#the-pretooluse-hook), never `{}`.
- `agy-session-start` injects a system message naming agy's role and session, the exact watcher
  command when no live watcher holds the pidfile, and the pending count.
- `agy-preinvocation` logs `lastUserInput` as a `console` line once: it skips blank input, the
  doorbell and `tandem:` lines, and input equal to `peers/agy/last_input`. The hook fires before
  every model call of a turn with the same input and the payload carries no known turn marker, so
  the same text typed twice in a row is logged once.
- `agy-stop`, only on `terminationReason` `NO_TOOL_CALL` with `fullyIdle`, returns `continue` to
  restart a dead watcher (at most once per 60 s, `peers/agy/watchdog`) and/or to read unrung mail;
  otherwise `{}`.

No-op outputs: `{"decision":"ask"}` for `PreToolUse`, `{}` for the others
([1.5.5 re-check](probes/2026-11-11-agy-1.5.5-recheck.md)).

## Implementation: Python 3.9+, stdlib only

The team runs macOS and Linux, so tandem is plain Python source that runs unchanged on both, with no
build step and no dependencies.

- **A `tandem/` package plus a `bin/tandem` shim** (`#!/usr/bin/env python3`) that resolves its own
  realpath and imports the package. Install is a clone plus a symlink (`make install`). Agents type
  plain `tandem …`, which the permission rules match. Hook logic is tested Python
  (`tandem hook <event>`), not generated shell.
- **3.9 floor**, because `/usr/bin/python3` from Apple's Command Line Tools is 3.9.6 and Linux
  distros ship 3.9–3.12. `env python3` picks whichever comes first on PATH.
- **Stdlib only**: no venv, no pip, no dependency on a teammate's machine.
- **No `zipapp`** (user's call). If distribution ever needs a single-file artifact instead of a
  clone, that's the point to revisit the language and move to a compiled binary (Go, Rust, or
  Swift), not to bundle Python. Swift caveat: Linux support is second-class compared to macOS.
- Startup is ~40 ms per call, fine even for agy's `PreToolUse` hook on every `run_command`.
- Exit codes: 0 ok; 1 any error, usage errors included (argparse's usual 2 would read as mail to
  Claude's waiter); 2 mail (`watch --once` only); 3 no mail or timeout (`recv`); 130 Ctrl-C.
- Rejected:
  - **Shell + `jq`**: the watcher and session resolution are state logic that gets fragile in shell,
    with no test tooling.
  - **Go**: a release pipeline for four targets, or Go on every teammate's machine, buys nothing v0
    needs. Reconsider if a single static artifact becomes important.

## Dashboard (`tandem dash`, later): stdlib is enough

Not designed yet (`docs/backlog/tandem-dash.md`); this only records that Python stdlib won't block it.

- **Push via SSE, not WebSockets.** `http.server.ThreadingHTTPServer` with a handler that sends
  `Content-Type: text/event-stream` and writes `data: …\n\n` + `flush()` in a loop.
- **Resume for free**: the byte offset in `log.jsonl` as the event `id`; `EventSource` reconnects
  with `Last-Event-ID`.
- **Change detection by polling** (`stat` every 0.5–1 s): the stdlib has no inotify/FSEvents, and
  polling behaves the same on macOS and Linux.
- **Browser → server is plain `POST`** (send as human, ack an escalation).
- WebSockets aren't in the stdlib; a hand-rolled RFC 6455 is ~150 lines for push nobody needs.
- Localhost only: bind `127.0.0.1`.

## Open questions

None blocking. Live-only or out of scope:

- Whether agy's `PreInvocation` payload carries a per-turn marker; if it does, key
  `peers/agy/last_input` on it so the same text typed twice is logged twice.
- Whether agy hooks see `ANTIGRAVITY_LS_ADDRESS` (on 1.5.4 they got only the conversation id), so a
  hook judges a watcher by its pid alone until the watcher's own LS check ends it.
- Whether agy's `plan` mode blocks running tests and shell writes.
