# Lens: smells

Structural problems that work today and cost later. Read the changed code and its immediate
neighbours.

Look for:

- Long methods doing several jobs; deep nesting; long parameter lists; boolean flag parameters
  that switch behaviour.
- A class or module that grew a second responsibility with this change.
- Feature envy: code that mostly works on another object's data.
- Primitive obsession: strings or hashes passed around where a small type or value object
  would name the concept.
- Magic numbers and strings, repeated literals.
- Inconsistency with the surrounding code: naming, error handling, file layout, patterns the
  codebase already settled on.
- Shotgun surgery: one concept changed in many places, a sign it needs one home.

Each finding says what to change, not only what smells. Most are `MINOR`.
