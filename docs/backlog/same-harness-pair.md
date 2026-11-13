---
worth: later
where: tandem/session.py (claim_role)
added: 2026-11-13
---
# Pairing two Claudes or two agys

A pair has to be one `claude` and one `agy`, even with an explicit `--role` on each launch. Mail and
the watcher state are keyed by role (`TANDEM_ROLE`, `peers/<role>/`), but the rest of the session is
keyed by peer name.

What breaks with `tandem claude --role driver` and then `tandem claude --role navigator`:

- `claim_role` sees `claude` holding driver, moves it to navigator and swaps the old navigator holder
  (`agy`) onto driver. `session.json` then says `driver: agy` while a Claude drives, which is wrong
  in the protocol's `other_peer`, `tandem status` and later claims.
- `claims.claude` is a single slot, so the second launch overwrites the first and the first Claude
  is left unclaimed. A third launch could take its role without being refused.
- Both share `peers/claude/settings.json`, `protocol.md` and `hooks.log`. The protocol text is safe
  (it goes in through argv), but if Claude re-reads its settings file mid-session the driver gets
  the navigator's permissions (unchecked).

Two agys are worse:

- `peers/agy/conversation_id` is one file that every agy hook writes, so both watchers inject into
  whichever agy fired a hook last.
- The write gate decides by `role_of(meta, "agy")` (`tandem/hooks.py`, `agy_pretool`), which is
  ambiguous when both roles are `agy`.
- `peers/agy/profile.json` is shared too.
- Two agy language servers side by side have never been probed.

Shape of the fix: key claims and peer dirs by role, have hooks take their role from `TANDEM_ROLE`
instead of `role_of(meta, peer)`, keep `roles` as role → peer and skip the swap when both peers have
the same name. For agy also keep `conversation_id` per role, and probe two agy instances in one repo
first. This would absorb `second-launcher-for-same-peer-not-refused.md`.

Deferred because nobody has needed a same-model pair yet, and such a pair loses most of the
second-opinion diversity that Claude plus agy gives.
