# Lens: implementation

Does the change do what the task and the plan asked, completely, and nothing else?

Look for:

- Each requirement in the brief, the ticket and `plan.md`: done, partly done or missing.
- Plan steps marked done whose code doesn't match what the step said.
- Decisions in `decisions.md` that the code doesn't follow.
- Wiring: a new class, route, job, setting or migration that nothing calls, registers or runs.
- Stubs, TODOs, placeholder values, debug output or commented-out code left behind.
- Callers of changed code that weren't updated (grep for them), and behaviour that changed for
  callers who didn't ask for it.
- Scope creep: changes the task didn't need. Name them; the driver keeps or drops them.
