# Working on tandem

For contributors and agents changing tandem itself. `README.md` is the user guide (install, usage,
every option). `docs/design.md` is the decision record: what tandem does, why, and what was
rejected. `docs/probes/` holds the dated probe results the design rests on, one file per probe. Read
the design before changing behaviour, and update it in the same change when a decision moves.

## Layout

```
bin/tandem                 shim: resolves its realpath, puts the checkout on sys.path, runs tandem.cli.main()
tandem/__init__.py         __version__ and TandemError (printed as `tandem: <msg>`, exit 1)
tandem/__main__.py         python3 -m tandem
tandem/cli.py              argparse, global --session, the command table and dispatch
tandem/paths.py            TANDEM_HOME, owner-only spool dirs, the shim path and quoted shim commands
tandem/session.py          naming, init, resolution, session.json and its state, repo pointers, the
                           session lock, log.jsonl, pidfiles, role claims, done/summary, harness env and
                           exec; shared helpers (now_iso, read_text, atomic writes, git, `seconds` for
                           CLI durations)
tandem/maildir.py          envelopes, id allocation, deliver (send), pop/peek (recv), rendering, log -f,
                           status
tandem/watch.py            doorbell text, rung markers; watch --once (Claude) and --detach (agy)
tandem/cmdline.py          sh-style lexer and the navigator write gate
tandem/hooks.py            `tandem hook <event>`: Claude's prompt log and the four agy hooks
tandem/protocol.py         renders templates/protocol.md for a peer and role (code-owned values such as
                           message types come in through placeholders); the launchers' initial prompt
                           and its prefix; the tandem subcommands and test prefixes each role runs
                           unprompted (one list for every rule)
tandem/templates/protocol.md  the protocol both peers get (shared, role and peer sections)
tandem/launch_claude.py    `tandem claude`: settings.json, protocol, argv/env, exec
tandem/launch_agy.py       `tandem agy`: agy version gate, hooks.json, rules, profiles, user deny carry-over
tandem/human.py            `tandem human` REPL
tests/                     unittest suite, one file per area; tests/helpers.py holds the fixture
docs/design.md             the decision record
docs/probes/               dated harness probes and live checks, one file each (index in README.md)
docs/plans/                implementation plans; docs/backlog/ deferred work
```

## Tests

- `make test` runs the suite with `python3` and then `/usr/bin/python3` (Apple's 3.9.6), skipping the
  second when it's absent. Both must pass. One file: `python3 -m unittest tests.test_send`.
- 3.9 is the floor: no `match`, no `X | Y` type unions, no 3.10+ stdlib APIs.
- Stdlib only. No pip, no venv, no third-party test or coverage tools (`python3 -m trace --count`
  if coverage is needed).

Conventions:

- Subclass `tests.helpers.TandemTestCase`. It gives each test a temp `HOME` and `TANDEM_HOME`,
  clears `TANDEM_SESSION`/`TANDEM_ROLE` and every `ANTIGRAVITY_*` variable, sets `TANDEM_AGY_PID` empty (so a
  detached watcher never looks for a real agy above a suite run from an agy pane), and isolates git from
  the user's config. `make_repo()` builds a fixture repo; `run_cli()` runs in-process, `run_shim()`
  runs the shim under the interpreter running the suite. Shared helpers live there too
  (`events`, `mark_ended`, `dead_pid`, `wait_for`, `FakeExec`); add one there rather than copying it
  into a second test file.
- Never touch the real `~/.tandem`, `~/.claude` or `~/.gemini`, and never start a real `claude` or
  `agy`. Harness boundaries are faked: a fake `claude`/`agy` on a test PATH, a fake agentapi through
  `ANTIGRAVITY_AGENTAPI_EXE`, a loopback listener standing in for agy's language server (`FakeLS`),
  and launchers take an injected `exec_fn`. Hooks are tested by feeding stdin JSON and checking
  stdout and the exit code.
- A test that starts a detached watcher kills it in teardown and asserts none is left
  (`pgrep -fl 'tandem watch'` should be empty after a run).
- `tandem watch --detach` has two argparse-suppressed flags, `--poll-interval` and
  `--ls-check-interval`, for tests only: the detached watcher is a separate process, so its
  intervals can't be patched in-process.

## How generated configs map to harness features

Launchers write everything into the session dir (`$TANDEM_HOME/sessions/<dir>/`), then `exec` the
harness. Nothing goes into the target repo or the user's global config.

- **Claude** (`tandem claude`): `peers/claude/settings.json` passed with `--settings` carries the
  Stop hook (`asyncRewake`, running `tandem watch --once`), the `UserPromptSubmit` hook
  (`tandem hook claude-prompt`) and the permissions. The rendered protocol goes in through
  `--append-system-prompt` (copy in `peers/claude/protocol.md`), since Claude ignores instructions
  that arrive as hook text. `--add-dir <session dir>` lets it read and, as driver, write the shared
  files.
- **agy** (`tandem agy`): `peers/agy/profile.json` passed with `--settings` is the whole permission
  policy (navigator or driver profile, deny merged with the user's global deny).
  `--add-dir <session dir>` makes agy load `.agents/hooks.json` and `.agents/rules/AGENTS.md` from
  it. `hooks.json` has a single top-level hook name, `tandem`, holding the events; event names at
  the top level load nothing. Its events come from `hooks.HOOKS` (each agy hook's `event`), so a hook
  is named once.
- Hook commands call the shim by absolute path; agents call plain `tandem …`, because permission
  rules match the command text.

## Debugging a session

Everything is in the session dir, `$TANDEM_HOME/sessions/<dir>/` (`tandem status` lists them):

- `peers/<peer>/hooks.log`: one line per hook failure (`claude` or `agy`); a hook always exits 0
  with its safe output, so this is the only trace.
- `peers/<role>/watch.log`: the agy watcher's own log (start, rings, failed `send-message` calls,
  exit reason); `peers/<role>/watch.pid` (`{pid, ls_address}`) and `peers/<role>/rung` (highest
  id rung) are its state, for Claude's waiter too.
- `peers/agy/profile.json` is the profile the last `tandem agy` launched with.
- `log.jsonl` (`tandem log`) holds the `agy_profile` event with agy's role and version.
- `MIN_AGY_VERSION` in `tandem/launch_agy.py` is the oldest agy tandem launches. Raise it only
  after a live check on the newer version, recorded as a new file in `docs/probes/`.

## Rules

- Comments justify decisions (why a value, why a workaround, why not the obvious option). They
  never narrate code. Context that doesn't fit a comment belongs here or in `docs/design.md`.
- Deferred work goes to `docs/backlog/<slug>.md`, one file per item (frontmatter `worth`, then
  `where` when it points at code, as `path (symbol)`, then `added`; H1 title; why it was deferred). `git rm` it in the change
  that lands it.
- `README.md` and `docs/design.md` must match the code: when behaviour changes, fix every line
  either contradicts (a new option or default changes the README's command tables too). Rejected
  alternatives stay in the design, labelled as such.
- A probe or live check gets a new `docs/probes/<date>-<subject>.md` and a row in its index. Probe
  files are history: don't rewrite one when a later version changes things; add a pointer to the
  newer probe instead.
