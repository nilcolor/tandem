---
worth: later
added: 2026-11-11
---
# agy sandbox and bypass behaviour never checked on Linux

Every sandbox, `unsandboxed(...)` bypass and forced-bypass result in the README comes from macOS
(agy 1.5.4). The team also runs Linux, where agy's terminal sandbox is a different implementation.
The launcher's self-check only confirms the rules load, not that the sandbox makes the workspace
read-only or that a bypass reaches loopback and Docker. Settles when a teammate reruns the sandbox
probe (S1–S8) and one bypassed test command on Linux: if it matches, delete this; if not, it becomes
a Linux-specific navigator profile.
