---
worth: yes
where: tandem/protocol.py (PEERS)
added: 2026-11-13
---
# Copilot CLI as a peer

The user wants GitHub's Copilot CLI to pair with Claude or agy, as driver or navigator. The design
already has room for it (`docs/design.md`, Launchers: new peers are a launcher plus a peer section
in the protocol template, and the bus, the CLI and the shared rules don't change). Today `PEERS`
knows only `claude` and `agy`, and `tests/test_protocol.py` pins `copilot` as an unknown peer.

What a `tandem copilot` launcher needs from the harness, none of it probed yet:

- **Instructions with user authority**: a launch-time way to hand it the rendered protocol (a system
  prompt flag, or an instructions file it loads from an added dir) without writing into the repo.
- **A wake path**: either a hook that can rewake an idle session (Claude's `asyncRewake`), or an
  external call that starts a turn in a running session (agy's `agentapi send-message`). Without one
  there's no doorbell, and this is the item's main unknown.
- **Permissions**: a per-session allowlist for `tandem …` and the test prefixes, and for the
  navigator a way to deny edits plus a pre-tool hook for the write gate (`cmdline.write_reason`).
- **Hooks for the logs**: a prompt-submit or equivalent hook for the human-input log.

Start with a probe of the installed Copilot CLI against that list, recorded in `docs/probes/`.
A pair is still one of each harness, so Copilot plus Claude or Copilot plus agy fits the existing
peer-keyed claims: `DEFAULT_ROLES` names only Claude and agy, so Copilot holds no role until it
launches with `--role`, and `claim_role` then replaces the idle default holder. Making Copilot a
default would need `init` to pick the pair. Two Copilots is `same-harness-pair.md`.
