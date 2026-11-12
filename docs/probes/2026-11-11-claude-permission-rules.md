# Claude Code permission rules, from the docs (2026-11-11)

From code.claude.com/docs/en/permissions and /settings:

- **Prefix rule: `Bash(tandem *)`.** A trailing ` *` also matches the bare command (`tandem`), and
  the space is part of the rule, so `tandemx` doesn't match. `Bash(tandem:*)` is an equivalent older
  spelling; the permission dialog itself writes the space form, so tandem does too, one rule per
  protocol subcommand (`Bash(tandem send *)`, …) rather than `Bash(tandem *)`.
- Compound commands (`&&`, `||`, `;`, `|`, `|&`, `&`, newline) need every part allowed. A leading
  assignment of a variable outside a known-safe set stops an allow rule matching
  (`TANDEM_ROLE=x tandem send` would prompt), and a rule matches the command text, so agents must
  call plain `tandem …`, not the shim by absolute path.
- A bare tool name in `deny` (`"Edit"`, `"Write"`, `"NotebookEdit"`) removes the tool from Claude's
  context entirely, which is what the navigator wants. Deny beats allow at every level.
- `--settings` sits above user, project and local settings, and **lists merge across scopes**
  (unlike agy): the user's own allow rules still apply inside a tandem session, and the navigator's
  deny rules can't be undone by them.

Confirmed live on 2.1.322: a navigator in the default (manual) mode ran `tandem send`, `recv` and
`status` with no prompt ([2026-11-12 live check](2026-11-12-agy-1.5.6-claude-2.1.322-live-check.md)).
