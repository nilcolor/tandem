---
worth: later
where: tandem/launch_agy.py (build_profile), tandem/hooks.py (agy_pretool)
added: 2026-11-11
---
# An allowlisted test prefix runs with any arguments

The agy navigator's `command(go test)` rule is a prefix match, so whatever follows it runs unasked
too: `go test -exec 'sh -c "…"' ./...` runs an arbitrary program, and `go test -o <path>` or
`-coverprofile=<path>` writes a file the write gate never sees (it only looks for writers and
redirects). `dip rspec` and `bundle exec rspec` take similar options, and Claude's
`Bash(go test *)` rules have the same shape. Deferred because the navigator runs the tests the
protocol names, the user watches its console, and the driver's `git status` check at each
checkpoint catches a stray file in the tree. The fix is a per-tool list of host-unsafe options
(`-exec`, `-toolexec`, `-o`, `-coverprofile`, `-cpuprofile`, ...) that the pretool hook turns into a
`force_ask`; revisit if live sessions show a navigator reaching for them.
rubocop already has this (`_rubocop_writes` in `tandem/cmdline.py`), as the model for the rest.
