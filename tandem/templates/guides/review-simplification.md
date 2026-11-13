# Lens: simplification

Is this the least code that does the job well?

Look for:

- Abstractions with one user: base classes, interfaces, factories, options or config nobody
  needs yet.
- Code that repeats something the codebase already has: find the existing helper, scope,
  concern or library call and name it.
- Duplication inside the change that one function would remove.
- Dead code: unused methods, parameters, branches, constants, imports.
- Indirection that doesn't pay: wrappers that only forward, layers that only rename.
- Hand-rolled versions of standard library or framework features.

Say what the simpler version is, not only that one exists.
