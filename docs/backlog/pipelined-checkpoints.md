---
worth: yes
where: tandem/templates/protocol.md (role driver, role navigator)
added: 2026-11-13
---
# The driver waits for every checkpoint review

The driver sends a `review_request` and waits for the review before starting the next step, so the
pair works one at a time. Pipelining would let the driver start the next step with at most one
checkpoint unreviewed (a `CHANGES` verdict means rework on top of whatever was built since, so a
wider window costs more), check `tandem recv` between steps (a Claude driver that never ends its
turn never hears the Stop-hook doorbell), and have the navigator do read-only prep for upcoming
steps while it waits. The navigator must still review a frozen tree: `--commit <rev>` gives one
when the driver commits each checkpoint; without commits it needs something like
`tandem send --snapshot` saving `git diff` as a patch at send time. Deferred until it's settled
whether drivers commit at checkpoints, which decides whether the snapshot is needed.
