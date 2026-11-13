# Lens: quality

Does the changed code behave correctly on every input it can get?

Look for:

- Logic errors: wrong conditions, off-by-one, inverted checks, a branch that can't be reached.
- Inputs at the edges: empty, nil/null, zero, negative, huge, unicode, duplicates, missing keys.
- Errors: swallowed or rescued too broadly, lost context, a failure reported as success, cleanup
  skipped on the error path.
- State: shared or global state changed, ordering assumptions, races between requests, jobs or
  threads, retries that aren't idempotent.
- Data: migrations that lose or lock data, queries that miss a scope or tenant, N+1 queries on a
  path that loops, transactions that commit half the work.
- Security: unchecked input reaching SQL, a shell, a path, HTML or a redirect; missing
  authorization; secrets in code or logs.
- Resources: files, connections or locks left open; unbounded growth; timeouts missing on calls
  that can hang.

Don't report style, naming or structure here; the other lenses cover them.
