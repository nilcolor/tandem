---
worth: later
added: 2026-11-11
---
# agy sandbox and bypass behaviour never checked on Linux

Every sandbox, `unsandboxed(...)` bypass and forced-bypass result in `docs/probes/` comes from macOS
(agy 1.5.4, and the 1.5.5 live probe). The team also runs Linux, where agy's terminal sandbox is a
different implementation. The launcher's self-check only confirms the profile parses, not what the
sandbox blocks (network, loopback, reads outside the workspace), whether it leaves the workspace
writable (the navigator write gate assumes it may), or that a bypass reaches loopback and Docker.
Settles when a teammate reruns the sandbox probe (`docs/probes/2026-11-10-agy-1.5.4-permissions-and-sandbox.md`
and the 1.5.5 live probe) and one bypassed test command on Linux: if it matches, delete this; if not, it becomes a
Linux-specific navigator profile.
