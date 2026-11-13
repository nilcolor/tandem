# tandem

Pair two coding agents on one task. One agent **drives** (plans and edits), the other **navigates**
(reviews and challenges), and you watch both in their own terminals and can type to either at any
time. Out of the box Claude Code drives and Antigravity CLI (`agy`) navigates; either can take either
role.

A session goes like this:

1. You give a task: a ticket key, a short brief, or just a first prompt to the driver.
2. The driver writes a plan; the navigator reviews it. They agree, or escalate to you.
3. The driver works in checkpoints; the navigator reviews each one from the diff and its own test
   runs.
4. The driver writes up what changed and why, asks you, and closes the session. tandem writes a
   `summary.md` with every message, every prompt you typed and the decisions.

The agents talk through a small mailbox on disk (`~/.tandem/sessions/<session>/`), not by pasting
into each other's prompts. When mail arrives, tandem rings a "doorbell" that wakes the idle agent.
Nothing is installed into your repo or your global Claude/agy config: everything is passed to the
harnesses at launch.

## Requirements

- macOS or Linux
- Python 3.9 or newer (`/usr/bin/python3` on macOS is fine); no packages to install
- `git`
- `claude` (Claude Code) and/or `agy` (Antigravity CLI) 1.5.6 or later on your PATH, logged in
  (`agy update` updates agy)

## Install

```sh
git clone <tandem repo> ~/code/tandem
cd ~/code/tandem
make install          # symlinks bin/tandem into ~/.local/bin
tandem --version
```

- `~/.local/bin` must be on your PATH. Put the link elsewhere with `make install BINDIR=/some/bin`.
- It's a symlink to the checkout, so `git pull` updates tandem; there's nothing to rebuild.
- `make uninstall` removes the link (only if it points at this checkout).
- `make test` runs the test suite.

## Quick start

In the repo you want to work on, on the task's branch:

```sh
tandem init                 # once per task; the session is named after the branch
tandem claude               # terminal 1: Claude, the driver
tandem agy                  # terminal 2: agy, the navigator
```

Then give the driver the task in its console (or pass it to `init`, see below). That's it: the pair
plans, reviews and implements on their own. Type to either agent whenever you like; everything you
type is recorded for the summary.

Useful while it runs, from any terminal in the repo:

```sh
tandem status               # pending mail, last messages, whether the watchers are up
tandem log -f               # follow the conversation
tandem human                # read escalations and answer them (optional)
```

When the work is done, the driver sends you its summary and asks whether to close the session. Say
yes and it runs `tandem done`, which writes `~/.tandem/sessions/<session>/summary.md`.

The first time in a new repo each harness asks whether you trust the folder. Answer it yourself.

## Commands

Run any command with `--help` for its options. Every command finds the session from the repo you're
in; `--session NAME` (before the command: `tandem --session fix-csv-bom status`) picks one
explicitly, by its short name or full directory name.

### `tandem init`: start a session

```sh
tandem init [name] [--ticket KEY] [--task TEXT | --task-file FILE] [--test-cmd CMD]... [--replace]
```

| Option | Meaning |
|---|---|
| `name` | Session name. Default: the branch name's last part (`feature/login-timeout` → `login-timeout`); with `--ticket`, the ticket key; on `main`, `master` or a detached HEAD, `<repo>-<MMDD-HHMM>`. |
| `--ticket KEY` | Ticket the task comes from. The driver starts intake from it. |
| `--task TEXT` | The task brief, inline. |
| `--task-file FILE` | The task brief, read from a file. |
| `--test-cmd CMD` | The command that runs the tests (repeatable). Both agents may run it without asking. Default: detected from the repo: `go.mod` → `go test`, `dip.yml` → `dip rspec`, a `Gemfile` without `dip.yml` → `bundle exec rspec`, and with either Ruby marker a `.rubocop.yml` adds `dip rubocop` or `bundle exec rubocop`. Giving any `--test-cmd` replaces detection. |
| `--replace` | End this repo's active session (without a summary) and start a new one. Run `tandem done` first if you want its summary. |

- A repo (more exactly, a checkout) has one active session at a time. Separate git worktrees get
  separate sessions, so parallel tasks in worktrees work.
- Names are lowercased to letters, digits and dashes for the directory; the name as you typed it is
  kept. Without a task given to `init`, the driver takes it from your first prompt.
- `init` prints the session directory.

### `tandem claude`: launch Claude Code

```sh
tandem claude [--role driver|navigator] [-- <claude arguments>]
```

| Option | Meaning |
|---|---|
| `--role` | Role to take. Default: the role the session gives Claude (driver, unless roles were flipped). Taking the other peer's role swaps the two, unless that peer is running; quit it first. |
| `-- …` | Anything after `--` goes to `claude` as is, e.g. `tandem claude -- --model opus`. |

Starts `claude` in this terminal with the session's protocol, hooks and permissions, and an opening
prompt starting with `tandem:` that tells it to check its mail and begin.

### `tandem agy`: launch Antigravity CLI

```sh
tandem agy [--role navigator|driver] [-- <agy arguments>]
```

| Option | Meaning |
|---|---|
| `--role` | Role to take. Default: the role the session gives agy (navigator, unless roles were flipped). |
| `-- …` | Anything after `--` goes to `agy` as is, e.g. `tandem agy -- --model <name>`. |

It needs agy 1.5.6 or later: an older agy is refused with a pointer to `agy update`, and a version
string tandem can't read gets a warning. On its first turn agy starts a small background watcher
(`tandem watch --as <role> --detach`), which rings it when mail arrives and exits when agy does.

What agy may do without asking:

- **As navigator**: the tandem commands it needs, the test commands and read-only git. Anything
  else asks you first, and anything that looks like a write gets a prompt saying
  `tandem write gate: …`. Its file-writing tool is denied.
- **As driver**: `accept-edits` mode (file edits don't ask), plus the tandem commands, the test
  commands and git. Other commands ask.

### `tandem human`: talk to the pair

```sh
tandem human
```

A small prompt for you. It prints escalations and questions sent to you as they arrive, and sends
what you type:

| Input | Sends |
|---|---|
| `text` | to the driver |
| `@driver text`, `@navigator text` | to that role, as an answer to the last message you read from it (or as a decision if there is none) |
| `@navigator:question text` | with an explicit type: `plan`, `review_request`, `review`, `question`, `answer`, `decision`, `escalate`, `done` |
| `/log`, `/status` | print the session log or status |
| `/help`, `/quit` | help; leave (Ctrl-D and Ctrl-C work too) |

One line is one message. It works from any directory when there's only one active session; inside
a repo it uses that repo's session. It exits when the session ends.

### `tandem status`, `tandem log`, `tandem done`

| Command | Does |
|---|---|
| `tandem status` | Every active session: pending mail per inbox, the last message from each role, whether each watcher is running, and stale repo pointers. With `--session`, just that one (ended or not). |
| `tandem log [--json] [-f]` | The session's history: messages, what you typed into each console, and events (launches, watchers, done). `--json` prints the raw records; `-f` keeps following until the session ends. |
| `tandem done` | Write `summary.md`, end the session and detach it from the repo. Normally the driver runs it after you confirm. After `done`, use `--session` to look at the ended session. |

### Commands the agents use

You rarely type these; the agents do, and their permissions allow them without prompts.

| Command | Does |
|---|---|
| `tandem send --to ROLE [--type T] [--reply-to ID] [--commit REV] [--file PATH]... [body]` | Send a message to `driver`, `navigator` or `human`. The body is the argument or stdin. `--type` defaults to `answer` with `--reply-to`, else `question`. `--expects-reply`/`--no-reply` override the default for the type. |
| `tandem recv [--peek] [--wait] [--timeout S] [--json]` | Print the oldest unread message and mark it read. Exits 3 when there's none (or on timeout). |
| `tandem watch --once [--deadline S]` / `--detach` | The doorbell watchers (Claude's and agy's); started by the launchers' hooks and by agy itself. |

Inside a launched harness the session and role come from the environment; outside one, add
`--as ROLE` to `send`, `recv` and `watch`.

Exit codes: 0 ok, 1 error, 2 mail (`watch --once`), 3 no mail or timeout (`recv`), 130 interrupted.

### Environment

| Variable | Meaning |
|---|---|
| `TANDEM_HOME` | Where sessions live. Default `~/.tandem`. |
| `TANDEM_SESSION`, `TANDEM_ROLE` | Set by the launchers inside each harness; you don't set them. |

## Roles and what they may do

- **Driver**: the only one that edits. Owns the plan (`plan.md`) and the decision log
  (`decisions.md`) in the session directory.
- **Navigator**: reads, reviews, runs the tests, never edits. This is enforced by permissions, not
  by asking nicely: Claude as navigator has no edit tools, and agy as navigator can't use its file
  tool and gets a prompt for any shell command that looks like a write.
- Either agent may run the tandem commands and the test commands without asking. Other commands
  follow each harness's own rules, so expect occasional prompts for read-only commands that aren't
  on the allowlist (`make help`, `go vet`, a long `grep`). Answer them once with "yes"; avoid the
  "always allow … (persist to settings.json)" options, which change your global config.
- As a backstop, the driver checks `git status` at each checkpoint and treats any change it didn't
  make as a navigator violation.

## Tips and known quirks

- **After `/new` in agy, type something once.** The watcher rings the new conversation, but agy
  doesn't act on it until that conversation has had a turn.
- **Claude's auto mode** (Shift+Tab) approves most commands by itself. tandem works the same with it
  on or off; with it off you'll see the occasional prompt for a command that isn't on the allowlist.
- **A prompt starting with `tandem:`** is the launcher's opening prompt, sent on your behalf. It's
  not logged as your input.
- **Quitting a harness** releases its role; relaunch it with the same command. agy's watcher stops
  by itself when agy exits, and the next agy launch starts a new one.
- **Flipping roles mid-task**: quit both, then `tandem agy --role driver` and
  `tandem claude --role navigator`.
- The agents' messages sometimes show a `tandem recv` as "failed" in agy: that's exit code 3, "no
  mail", and expected.

## Troubleshooting

Everything for a session is in `~/.tandem/sessions/<session>/` (`tandem init` and `tandem status`
show the path):

| File | What it tells you |
|---|---|
| `log.jsonl` (`tandem log`) | every message, console input and event; the `agy_profile` event records agy's role and version at launch |
| `peers/<claude\|agy>/hooks.log` | one line per hook failure; empty is good |
| `peers/<role>/watch.log` | agy's watcher: start, which agy process it serves, each ring, failures, why it exited |
| `peers/<role>/watch.pid` | the running watcher's pid |
| `summary.md` | written by `tandem done` |

- **agy doesn't wake on mail**: `tandem status` shows whether its watcher is running. If not, tell
  agy "start your tandem watcher"; it knows the command. Then check `watch.log`.
- **agy prompts on every tandem command**: check that the agents type plain `tandem …` (the rules
  match the command text) and that agy is 1.5.6 or later (`tandem log | grep agy_profile` shows the
  version it launched with).
- **`tandem init` refuses**: the repo already has an active session; finish it with `tandem done` or
  start over with `tandem init --replace`.
- **"no active session"**: you're outside the repo or the session has ended; use `--session NAME`.

## More

- [`docs/design.md`](docs/design.md): how tandem works and why, including rejected alternatives.
- [`docs/probes/`](docs/probes/README.md): dated records of what each Claude and agy version
  actually did when tested.
- [`AGENTS.md`](AGENTS.md): working on tandem itself (layout, tests, conventions).
- `docs/backlog/`: known gaps and deferred work.
