# Probes

Dated records of what a harness actually did when tandem's assumptions were tested: one file per
probe or live check, named `<date>-<subject>.md`, never edited after the fact except to add a
pointer to a later probe that overturned it. `docs/design.md` states the current decisions and links
here for the evidence. A new agy or Claude release that needs re-checking gets a new file.

| File | What it settled |
|---|---|
| [2026-11-10-agy-1.5.4-agentapi-wake.md](2026-11-10-agy-1.5.4-agentapi-wake.md) | `agy agentapi send-message` wakes an idle TUI; mid-turn queueing; `/new`; where a watcher can live |
| [2026-11-10-claude-asyncrewake.md](2026-11-10-claude-asyncrewake.md) | Claude's Stop hook with `asyncRewake` wakes an idle session; the doorbell needs launch-time authority |
| [2026-11-10-agy-1.5.4-permissions-and-sandbox.md](2026-11-10-agy-1.5.4-permissions-and-sandbox.md) | `--settings` permissions, the sandbox, `unsandboxed(...)` bypass, the `PreToolUse` contract, `--add-dir` customizations |
| [2026-11-11-agy-1.5.5-recheck.md](2026-11-11-agy-1.5.5-recheck.md) | print-mode re-check of the profiles on 1.5.5; `deny` replacement; `/config` doesn't validate rules |
| [2026-11-11-agy-1.5.5-live-probe.md](2026-11-11-agy-1.5.5-live-probe.md) | `unsandboxed(...)` still works on 1.5.5; the workspace is writable in the sandbox |
| [2026-11-11-claude-permission-rules.md](2026-11-11-claude-permission-rules.md) | Claude Code permission rule syntax (from the docs) |
| [2026-11-12-agy-1.5.6-claude-2.1.322-live-check.md](2026-11-12-agy-1.5.6-claude-2.1.322-live-check.md) | the end-to-end live check (plan Task 17): 1.5.6 dropped `unsandboxed(...)`, and the fixes that followed |
