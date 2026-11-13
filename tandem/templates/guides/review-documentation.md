# Lens: documentation

Will the next reader understand this change, and is everything written about it still true?

Look for:

- README, docs, guides, changelogs or API docs that the change made wrong, and new behaviour,
  options, settings or env vars they don't mention.
- Comments that now contradict the code, and comments that only restate it.
- A decision that isn't obvious from the code (a workaround, a pinned version, a rejected
  option) with nothing recording why.
- Names that mislead: a method whose name says less or more than it does.
- Commit messages or the summary describing something other than what changed.

Follow the repo's own conventions for where docs live (AGENTS.md, CLAUDE.md, CONTRIBUTING).
