---
worth: later
where: tandem/hooks.py (agy_pretool), tandem/launch_agy.py (build_profile)
added: 2026-11-11
---
# A bypassed test prefix runs on the host with any arguments

The agy navigator's `unsandboxed(go test)` rule and the hook's forced bypass match the prefix, so
whatever follows it runs outside the sandbox too: `go test -exec 'sh -c "…"' ./...` runs an
arbitrary program on the host, and `go test -o <path>` or `-coverprofile=<path>` writes a file the
write gate never sees (it only looks for writers and redirects). `dip rspec` and
`bundle exec rspec` take similar options. Deferred because the navigator runs the tests the
protocol names, the user watches its console, and the driver's `git status` check at each
checkpoint catches a stray file in the tree. The fix is a per-tool list of host-unsafe options
(`-exec`, `-toolexec`, `-o`, `-coverprofile`, `-cpuprofile`, ...) that turns the call into a
`force_ask` instead of a bypass; revisit if live sessions show a navigator reaching for them.
