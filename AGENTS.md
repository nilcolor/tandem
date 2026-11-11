# Working on tandem

For contributors and agents changing tandem itself. `README.md` is the decision record: what tandem
does, why, what was rejected, and the probe results the design rests on. Read it before changing
behaviour, and update it in the same change when a decision moves.

## Layout

```
bin/tandem                 shim: resolves its realpath, puts the checkout on sys.path, runs tandem.cli.main()
tandem/__init__.py         __version__ and TandemError (printed as `tandem: <msg>`, exit 1)
tandem/__main__.py         python3 -m tandem
tandem/cli.py              argparse, global --session, the command registry and dispatch
tandem/paths.py            TANDEM_HOME, spool dirs, the shim path and quoted shim commands for hooks
tandem/session.py          naming, init, resolution, session.json, repo pointers, the session lock,
                           log.jsonl, pidfiles, role claims, done/summary, status, exec of a harness
tandem/maildir.py          envelopes, id allocation, deliver (send), pop/peek (recv), rendering, log -f
tandem/watch.py            doorbell text, rung markers; watch --once (Claude) and --detach (agy)
tandem/cmdline.py          sh-style lexer, bypass prefix match and the navigator write gate
tandem/hooks.py            `tandem hook <event>`: Claude's prompt log and the four agy hooks
tandem/protocol.py         renders templates/protocol.md for a peer and role
tandem/templates/protocol.md  the protocol both peers get (shared, role and peer sections)
tandem/launch_claude.py    `tandem claude`: settings.json, protocol, argv/env, exec
tandem/launch_agy.py       `tandem agy`: hooks.json, rules, profiles, user deny carry-over, self-check
tandem/human.py            `tandem human` REPL
tests/                     unittest suite, one file per area; tests/helpers.py holds the fixture
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
  clears `TANDEM_SESSION`/`TANDEM_ROLE` and every `ANTIGRAVITY_*` variable, and isolates git from
  the user's config. `make_repo()` builds a fixture repo; `run_cli()` runs in-process, `run_shim()`
  runs the shim under the interpreter running the suite.
- Never touch the real `~/.tandem`, `~/.claude` or `~/.gemini`, and never start a real `claude` or
  `agy`. Harness boundaries are faked: a fake `claude`/`agy` on a test PATH, a fake agentapi through
  `ANTIGRAVITY_AGENTAPI_EXE`, a loopback listener standing in for agy's language server (`FakeLS`),
  and launchers take an injected `exec_fn`. Hooks are tested by feeding stdin JSON and checking
  stdout and the exit code.
- A test that starts a detached watcher kills it in teardown and asserts none is left
  (`pgrep -fl 'tandem watch'` should be empty after a run).

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
  policy (navigator, fallback or driver profile, deny merged with the user's global deny).
  `--add-dir <session dir>` makes agy load `.agents/hooks.json` and `.agents/rules/AGENTS.md` from
  it. `hooks.json` has a single top-level hook name, `tandem`, holding the events; event names at
  the top level load nothing.
- Hook commands call the shim by absolute path; agents call plain `tandem …`, because permission
  rules match the command text.

## Rules

- Comments justify decisions (why a value, why a workaround, why not the obvious option). They
  never narrate code. Context that doesn't fit a comment belongs here or in README.md.
- Deferred work goes to `docs/backlog/<slug>.md`, one file per item (frontmatter `worth`, `where`,
  `added`; H1 title; why it was deferred). `git rm` it in the change that lands it.
- README.md must match the code: when behaviour changes, fix every line it contradicts. Probe
  records and rejected alternatives stay, labelled as history.
