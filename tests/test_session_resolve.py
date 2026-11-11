import os

from tandem import TandemError, cli, session
from tests.helpers import TandemTestCase


class ResolveTestCase(TandemTestCase):
    def setUp(self):
        super().setUp()
        self.plain = os.path.join(self.tmp, "plain")
        os.makedirs(self.plain)
        os.environ["GIT_CEILING_DIRECTORIES"] = self.tmp

    def init(self, repo, name=None, **kw):
        return session.init_session(cwd=repo, name=name, **kw)["dir"]

    def end(self, dir_name):
        meta = session.load_session(dir_name)
        meta["ended"] = "2026-11-11T12:00:00Z"
        session.save_session(meta)

    def resolve(self, explicit=None, cwd=None):
        return session.resolve_session(explicit, cwd=cwd or self.plain)["dir"]

    def resolve_error(self, explicit=None, cwd=None):
        with self.assertRaises(TandemError) as ctx:
            session.resolve_session(explicit, cwd=cwd or self.plain)
        return str(ctx.exception)


class PrecedenceTest(ResolveTestCase):
    def setUp(self):
        super().setUp()
        self.repo_a = self.make_repo("alpha")
        self.repo_b = self.make_repo("beta")
        self.a = self.init(self.repo_a, "one")
        self.b = self.init(self.repo_b, "two")

    def test_explicit_beats_env(self):
        os.environ["TANDEM_SESSION"] = self.a
        self.assertEqual(self.resolve("beta--two", cwd=self.repo_a), self.b)

    def test_env_beats_repo_pointer(self):
        os.environ["TANDEM_SESSION"] = self.b
        self.assertEqual(self.resolve(cwd=self.repo_a), self.b)

    def test_repo_pointer_beats_the_rest(self):
        self.assertEqual(self.resolve(cwd=self.repo_a), self.a)
        self.assertEqual(self.resolve(cwd=self.repo_b), self.b)

    def test_only_active_session_outside_a_repo(self):
        self.end(self.b)
        self.assertEqual(self.resolve(), self.a)

    def test_empty_env_is_unset(self):
        os.environ["TANDEM_SESSION"] = ""
        self.assertEqual(self.resolve(cwd=self.repo_a), self.a)

    def test_resolve_args_uses_global_session_option(self):
        args = cli.build_parser().parse_args(["--session", "two", "init"])
        os.chdir(self.repo_a)
        self.assertEqual(session.resolve_args(args)["dir"], self.b)

    def test_resolve_args_without_option_uses_pointer(self):
        args = cli.build_parser().parse_args(["init"])
        os.chdir(self.repo_a)
        self.assertEqual(session.resolve_args(args)["dir"], self.a)


class ExplicitNameTest(ResolveTestCase):
    def setUp(self):
        super().setUp()
        self.repo_a = self.make_repo("alpha")
        self.repo_b = self.make_repo("beta")

    def test_full_dir_name_from_anywhere(self):
        a = self.init(self.repo_a, "fix")
        self.assertEqual(self.resolve("alpha--fix"), a)
        self.assertEqual(self.resolve("alpha--fix", cwd=self.repo_b), a)

    def test_repo_local_short_name_wins_over_other_repos(self):
        a = self.init(self.repo_a, "fix")
        b = self.init(self.repo_b, "fix")
        self.assertEqual(self.resolve("fix", cwd=self.repo_a), a)
        self.assertEqual(self.resolve("fix", cwd=self.repo_b), b)

    def test_repo_local_short_name_from_subdirectory(self):
        self.init(self.repo_a, "fix")
        b = self.init(self.repo_b, "fix")
        sub = os.path.join(self.repo_b, "deep", "er")
        os.makedirs(sub)
        self.assertEqual(self.resolve("fix", cwd=sub), b)

    def test_globally_unique_short_name(self):
        a = self.init(self.repo_a, "fix")
        self.init(self.repo_b, "other")
        self.assertEqual(self.resolve("fix"), a)
        self.assertEqual(self.resolve("fix", cwd=self.repo_b), a)

    def test_short_name_is_sanitized_before_matching(self):
        a = self.init(self.repo_a, "fix-csv-bom")
        self.assertEqual(self.resolve("Fix CSV BOM"), a)

    def test_collision_suffix_is_part_of_the_short_name(self):
        self.init(self.repo_a, "fix")
        self.end("alpha--fix")
        second = self.init(self.repo_a, "fix")
        self.assertEqual(second, "alpha--fix-2")
        self.assertEqual(self.resolve("fix-2"), second)

    def test_ambiguous_short_name_outside_a_repo(self):
        self.init(self.repo_a, "fix")
        self.init(self.repo_b, "fix")
        err = self.resolve_error("fix")
        self.assertIn("ambiguous", err)
        self.assertIn("alpha--fix (%s)" % self.repo_a, err)
        self.assertIn("beta--fix (%s)" % self.repo_b, err)

    def test_ambiguous_short_name_prefers_the_active_one(self):
        self.init(self.repo_a, "fix")
        b = self.init(self.repo_b, "fix")
        self.end("alpha--fix")
        self.assertEqual(self.resolve("fix"), b)

    def test_ambiguous_ended_short_names_list_all(self):
        self.init(self.repo_a, "fix")
        self.init(self.repo_b, "fix")
        self.end("alpha--fix")
        self.end("beta--fix")
        err = self.resolve_error("fix")
        self.assertIn("alpha--fix", err)
        self.assertIn("beta--fix", err)

    def test_explicit_name_returns_an_ended_session(self):
        a = self.init(self.repo_a, "fix")
        self.end(a)
        meta = session.resolve_session("fix", cwd=self.repo_a)
        self.assertEqual(meta["dir"], a)
        self.assertIsNotNone(meta["ended"])

    def test_unknown_name(self):
        self.init(self.repo_a, "fix")
        self.assertEqual(self.resolve_error("nope"), "no session named 'nope'")

    def test_name_without_letters(self):
        self.assertEqual(self.resolve_error("--"), "no session named '--'")

    def test_path_like_name_is_not_a_dir_name(self):
        self.init(self.repo_a, "fix")
        self.assertIn("no session named", self.resolve_error("../sessions/alpha--fix"))

    def test_corrupt_session_json_named_explicitly(self):
        a = self.init(self.repo_a, "fix")
        with open(os.path.join(session.session_path(a), "session.json"), "w") as f:
            f.write("{not json")
        self.assertIn("cannot read", self.resolve_error(a))


class EnvTest(ResolveTestCase):
    def test_env_names_a_missing_session(self):
        repo = self.make_repo()
        self.init(repo, "fix")
        os.environ["TANDEM_SESSION"] = "repo--gone"
        self.assertEqual(self.resolve_error(cwd=repo), "TANDEM_SESSION names no session: repo--gone")

    def test_env_returns_an_ended_session(self):
        repo = self.make_repo()
        d = self.init(repo, "fix")
        self.end(d)
        os.environ["TANDEM_SESSION"] = d
        self.assertEqual(self.resolve(), d)

    def test_env_path_is_rejected(self):
        os.environ["TANDEM_SESSION"] = "../x"
        self.assertIn("TANDEM_SESSION names no session", self.resolve_error())


class RepoPointerTest(ResolveTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo(branch="feature/first")
        self.first = self.init(self.repo)

    def test_subdirectory_resolves_through_toplevel(self):
        sub = os.path.join(self.repo, "a", "b")
        os.makedirs(sub)
        self.assertEqual(self.resolve(cwd=sub), self.first)

    def test_worktree_does_not_inherit_the_main_checkout_session(self):
        wt = os.path.join(self.tmp, "repo-wt")
        self.git(self.repo, "worktree", "add", "-q", "-b", "feature/second", wt)
        err = self.resolve_error(cwd=wt)
        self.assertIn("no active session for %s" % wt, err)
        self.assertIn(self.first, err)

    def test_worktree_resolves_its_own_session(self):
        wt = os.path.join(self.tmp, "repo-wt")
        self.git(self.repo, "worktree", "add", "-q", "-b", "feature/second", wt)
        second = self.init(wt)
        self.assertEqual(self.resolve(cwd=wt), second)
        self.assertEqual(self.resolve(cwd=self.repo), self.first)

    def test_ended_pointer_is_stale(self):
        self.end(self.first)
        err = self.resolve_error(cwd=self.repo)
        self.assertIn("no active session for %s" % self.repo, err)
        self.assertIn("stale repo pointer: %s (ended)" % self.first, err)

    def test_unreadable_pointer_target_is_stale(self):
        with open(os.path.join(session.session_path(self.first), "session.json"), "w") as f:
            f.write("{")
        err = self.resolve_error(cwd=self.repo)
        self.assertIn("no active session for %s; run `tandem init`" % self.repo, err)
        self.assertIn("stale repo pointer: %s (unreadable)" % self.first, err)

    def test_missing_pointer_target_is_stale(self):
        os.rename(session.session_path(self.first), os.path.join(self.tmp, "gone"))
        err = self.resolve_error(cwd=self.repo)
        self.assertIn("stale repo pointer: %s (missing)" % self.first, err)

    def test_stale_pointer_does_not_fall_back_to_another_repo(self):
        other = self.make_repo("other")
        o = self.init(other, "elsewhere")
        self.end(self.first)
        err = self.resolve_error(cwd=self.repo)
        self.assertIn("stale repo pointer: %s (ended)" % self.first, err)
        self.assertIn("active sessions (pick one with --session):", err)
        self.assertIn("  %s (%s)" % (o, other), err)

    def test_repo_without_session_does_not_take_the_only_active_one(self):
        other = self.make_repo("other")
        err = self.resolve_error(cwd=other)
        self.assertIn("no active session for %s; run `tandem init`" % other, err)
        self.assertNotIn("stale", err)
        self.assertIn(self.first, err)


class AmbiguityTest(ResolveTestCase):
    def test_two_active_sessions_outside_a_repo(self):
        a = self.init(self.make_repo("alpha"), "one")
        b = self.init(self.make_repo("beta"), "two")
        err = self.resolve_error()
        self.assertIn("not inside a git repository and 2 active sessions", err)
        self.assertIn("  %s (%s)" % (a, os.path.join(self.tmp, "alpha")), err)
        self.assertIn("  %s (%s)" % (b, os.path.join(self.tmp, "beta")), err)

    def test_no_sessions_outside_a_repo(self):
        err = self.resolve_error()
        self.assertEqual(
            err, "not inside a git repository and no active session; run `tandem init` in a repo"
        )

    def test_only_ended_sessions_outside_a_repo(self):
        a = self.init(self.make_repo(), "fix")
        self.end(a)
        self.assertIn("no active session", self.resolve_error())

    def test_corrupt_session_is_ignored_when_listing(self):
        a = self.init(self.make_repo("alpha"), "one")
        b = self.init(self.make_repo("beta"), "two")
        with open(os.path.join(session.session_path(b), "session.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(self.resolve(), a)

    def test_stray_dir_without_session_json_is_ignored(self):
        a = self.init(self.make_repo(), "fix")
        os.makedirs(session.session_path("junk"))
        self.assertEqual([m["dir"] for m in session.all_sessions()], [a])
        self.assertEqual(self.resolve(), a)


class ShortNameTest(TandemTestCase):
    def test_short_name(self):
        self.assertEqual(session.short_name("apix--fix-csv-bom"), "fix-csv-bom")
        self.assertEqual(session.short_name("repo--fix-2"), "fix-2")
        self.assertEqual(session.short_name("junk"), "junk")
