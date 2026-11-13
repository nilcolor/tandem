# Lens: testing

Would the tests catch it if this change broke?

Look for:

- New or changed behaviour without a test, including each error path and edge case the quality
  lens found.
- Tests that pass without the change: they assert too little, mock the thing under test, or
  check a value the test itself set.
- Tests tied to implementation details (private methods, call counts, exact SQL) where behaviour
  could be checked instead.
- Order or time dependence: shared fixtures, real clocks, random data without a seed, sleeps,
  network calls.
- Fixtures and factories changed in ways that quietly alter other tests.
- The suite itself: run the test commands yourself, and judge a partial run by its
  examples/failures line.
