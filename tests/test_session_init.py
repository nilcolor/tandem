import datetime
import hashlib
import json
import os
from unittest import mock

from tandem import paths, session
from tests.helpers import TandemTestCase

FIXED = datetime.datetime(2026, 11, 11, 10, 5, tzinfo=datetime.timezone.utc)
STAMP = FIXED.astimezone().strftime("%m%d-%H%M")


class InitTestCase(TandemTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("tandem.session._now", return_value=FIXED)
        patcher.start()
        self.addCleanup(patcher.stop)

    def init(self, *argv, cwd=None):
        """Run `tandem init` in `cwd`; returns (code, stdout, stderr)."""
        os.chdir(cwd or self.repo)
        return self.run_cli("init", *argv)

    def init_ok(self, *argv, cwd=None):
        code, out, err = self.init(*argv, cwd=cwd)
        self.assertEqual(code, 0, err)
        sdir = out.strip()
        return json.loads(self.read(sdir, "session.json"))

    def read(self, *parts):
        with open(os.path.join(*parts), encoding="utf-8") as f:
            return f.read()

    def log_lines(self, dir_name):
        text = self.read(session.session_path(dir_name), "log.jsonl")
        return [json.loads(line) for line in text.splitlines()]

    def session_dirs(self):
        return sorted(os.listdir(paths.sessions_dir()))


class SanitizeTest(TandemTestCase):
    def test_lowercases_and_collapses_runs(self):
        self.assertEqual(session.sanitize("Fix  CSV__BOM!!"), "fix-csv-bom")

    def test_trims_dashes(self):
        self.assertEqual(session.sanitize("--hello--"), "hello")

    def test_accents_keep_their_letters(self):
        self.assertEqual(session.sanitize("Café über naïve"), "cafe-uber-naive")

    def test_non_latin_drops_out(self):
        self.assertEqual(session.sanitize("日本語"), "")
        self.assertEqual(session.sanitize("fix 日本 bug"), "fix-bug")

    def test_cap_48_and_no_trailing_dash_after_cut(self):
        self.assertEqual(session.sanitize("a" * 60), "a" * 48)
        self.assertEqual(session.sanitize("a" * 47 + " b"), "a" * 47)


class NamingTest(InitTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo(branch="feature/login-timeout")

    def test_feature_branch_uses_part_after_last_slash(self):
        meta = self.init_ok()
        self.assertEqual(meta["name"], "login-timeout")
        self.assertEqual(meta["dir"], "repo--login-timeout")

    def test_branch_without_slash(self):
        self.git(self.repo, "checkout", "-q", "-b", "Quick_Fix")
        self.assertEqual(self.init_ok()["dir"], "repo--quick-fix")

    def test_main_uses_repo_and_timestamp(self):
        self.git(self.repo, "checkout", "-q", "main")
        meta = self.init_ok()
        self.assertEqual(meta["name"], "repo-%s" % STAMP)
        self.assertEqual(meta["dir"], "repo--repo-%s" % STAMP)

    def test_master_uses_repo_and_timestamp(self):
        self.git(self.repo, "checkout", "-q", "-b", "master")
        self.assertEqual(self.init_ok()["name"], "repo-%s" % STAMP)

    def test_detached_head_uses_repo_and_timestamp(self):
        self.git(self.repo, "checkout", "-q", "--detach")
        self.assertEqual(self.init_ok()["name"], "repo-%s" % STAMP)

    def test_branch_with_no_usable_characters_falls_back_to_timestamp(self):
        self.git(self.repo, "checkout", "-q", "-b", "feature/日本語")
        self.assertEqual(self.init_ok()["name"], "repo-%s" % STAMP)

    def test_free_text_name_kept_as_typed_and_sanitized_on_disk(self):
        meta = self.init_ok("Fix CSV BOM!")
        self.assertEqual(meta["name"], "Fix CSV BOM!")
        self.assertEqual(meta["dir"], "repo--fix-csv-bom")

    def test_unicode_and_punctuation(self):
        meta = self.init_ok("Café: über/naïve (v2)")
        self.assertEqual(meta["name"], "Café: über/naïve (v2)")
        self.assertEqual(meta["dir"], "repo--cafe-uber-naive-v2")

    def test_long_name_capped(self):
        meta = self.init_ok("x" * 80)
        self.assertEqual(meta["dir"], "repo--" + "x" * 48)

    def test_name_with_no_usable_characters_is_an_error(self):
        code, out, err = self.init("日本語")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("no letters or digits", err)
        self.assertFalse(os.path.exists(paths.sessions_dir()))

    def test_ticket_is_the_default_name(self):
        meta = self.init_ok("--ticket", "APIX-123")
        self.assertEqual(meta["name"], "APIX-123")
        self.assertEqual(meta["dir"], "repo--apix-123")
        self.assertEqual(meta["ticket"], "APIX-123")

    def test_explicit_name_wins_over_ticket(self):
        meta = self.init_ok("fix-csv-bom", "--ticket", "APIX-123")
        self.assertEqual(meta["dir"], "repo--fix-csv-bom")
        self.assertEqual(meta["ticket"], "APIX-123")

    def test_repo_name_sanitized_in_dir(self):
        repo = self.make_repo(name="My Repo.v2", branch="feature/x")
        self.assertEqual(self.init_ok(cwd=repo)["dir"], "my-repo-v2--x")

    def test_collision_in_same_repo_appends_suffix(self):
        self.assertEqual(self.init_ok("same")["dir"], "repo--same")
        self.assertEqual(self.init_ok("same", "--replace")["dir"], "repo--same-2")
        self.assertEqual(self.init_ok("same", "--replace")["dir"], "repo--same-3")

    def test_collision_across_repos_with_same_basename(self):
        self.init_ok("same")
        twin = os.path.join(self.tmp, "elsewhere", "repo")
        os.makedirs(twin)
        self.git(twin, "init", "-q", "-b", "main")
        self.assertEqual(self.init_ok("same", cwd=twin)["dir"], "repo--same-2")
        self.assertEqual(self.session_dirs(), ["repo--same", "repo--same-2"])


class TestCmdTest(InitTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()

    def touch(self, name):
        open(os.path.join(self.repo, name), "w").close()

    def test_no_markers(self):
        self.assertEqual(self.init_ok()["test_cmds"], [])

    def test_go_mod(self):
        self.touch("go.mod")
        self.assertEqual(self.init_ok()["test_cmds"], ["go test"])

    def test_dip_yml_wins_over_gemfile(self):
        self.touch("dip.yml")
        self.touch("Gemfile")
        self.assertEqual(self.init_ok()["test_cmds"], ["dip rspec"])

    def test_gemfile_without_dip(self):
        self.touch("Gemfile")
        self.assertEqual(self.init_ok()["test_cmds"], ["bundle exec rspec"])

    def test_several_markers(self):
        self.touch("go.mod")
        self.touch("Gemfile")
        self.assertEqual(self.init_ok()["test_cmds"], ["go test", "bundle exec rspec"])

    def test_detected_at_toplevel_from_subdirectory(self):
        self.touch("go.mod")
        sub = os.path.join(self.repo, "pkg", "inner")
        os.makedirs(sub)
        meta = self.init_ok(cwd=sub)
        self.assertEqual(meta["test_cmds"], ["go test"])
        self.assertEqual(meta["repo"], self.repo)

    def test_test_cmd_replaces_detection(self):
        self.touch("go.mod")
        meta = self.init_ok("--test-cmd", "make test", "--test-cmd", " go test -race ")
        self.assertEqual(meta["test_cmds"], ["make test", "go test -race"])

    def test_empty_test_cmd_rejected(self):
        code, _, err = self.init("--test-cmd", " ")
        self.assertEqual(code, 1)
        self.assertIn("--test-cmd", err)


class TreeTest(InitTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo(branch="feature/login-timeout")

    def test_prints_session_dir(self):
        code, out, _ = self.init()
        self.assertEqual(code, 0)
        self.assertEqual(out, session.session_path("repo--login-timeout") + "\n")

    def test_tree_layout(self):
        meta = self.init_ok()
        sdir = session.session_path(meta["dir"])
        for role in ("driver", "navigator", "human"):
            for sub in ("tmp", "new", "cur"):
                path = os.path.join(sdir, "inbox", role, sub)
                self.assertTrue(os.path.isdir(path), path)
                self.assertEqual(os.listdir(path), [])
        self.assertEqual(os.listdir(os.path.join(sdir, "peers")), [])
        self.assertTrue(os.path.isfile(os.path.join(sdir, "lock")))

    def test_session_json(self):
        meta = self.init_ok()
        self.assertEqual(
            meta,
            {
                "name": "login-timeout",
                "dir": "repo--login-timeout",
                "repo": self.repo,
                "ticket": None,
                "task": None,
                "test_cmds": [],
                "roles": {"driver": "claude", "navigator": "agy"},
                "created": "2026-11-11T10:05:00Z",
                "ended": None,
                "replaced_by": None,
            },
        )

    def test_repo_pointer(self):
        self.init_ok()
        key = hashlib.sha256(self.repo.encode("utf-8")).hexdigest()[:16]
        pdir = os.path.join(paths.repos_dir(), key)
        self.assertEqual(self.read(pdir, "current"), "repo--login-timeout\n")
        self.assertEqual(self.read(pdir, "path"), self.repo + "\n")
        self.assertEqual(session.read_pointer(self.repo), "repo--login-timeout")

    def test_pointer_keyed_by_realpath(self):
        link = os.path.join(self.tmp, "link-to-repo")
        os.symlink(self.repo, link)
        self.init_ok(cwd=link)
        self.assertEqual(session.read_pointer(self.repo), "repo--login-timeout")

    def test_log_has_only_the_init_event(self):
        self.init_ok()
        self.assertEqual(
            self.log_lines("repo--login-timeout"),
            [
                {
                    "kind": "event",
                    "ts": "2026-11-11T10:05:00Z",
                    "event": "init",
                    "session": "repo--login-timeout",
                    "repo": self.repo,
                }
            ],
        )

    def test_through_the_shim(self):
        result = self.run_shim("init", "via shim", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, session.session_path("repo--via-shim") + "\n")


class TaskTest(InitTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()

    def test_task_text(self):
        self.assertEqual(self.init_ok("--task", "Fix the BOM in CSV export")["task"], "Fix the BOM in CSV export")

    def test_task_file(self):
        brief = os.path.join(self.tmp, "brief.md")
        with open(brief, "w", encoding="utf-8") as f:
            f.write("# Brief\n\nStrip the BOM — then test.\n")
        self.assertEqual(self.init_ok("--task-file", brief)["task"], "# Brief\n\nStrip the BOM — then test.\n")

    def test_blank_task_stored_as_null(self):
        self.assertIsNone(self.init_ok("--task", "  \n")["task"])

    def test_task_and_task_file_are_exclusive(self):
        code, _, err = self.init("--task", "x", "--task-file", "y")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("tandem: "), err)

    def test_missing_task_file(self):
        missing = os.path.join(self.tmp, "nope.md")
        code, out, err = self.init("--task-file", missing)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("cannot read --task-file %s" % missing, err)
        self.assertFalse(os.path.exists(paths.sessions_dir()))

    def test_task_file_is_a_directory(self):
        code, _, err = self.init("--task-file", self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("cannot read --task-file", err)

    def test_task_file_not_utf8(self):
        brief = os.path.join(self.tmp, "latin1.md")
        with open(brief, "wb") as f:
            f.write(b"caf\xe9\n")
        code, _, err = self.init("--task-file", brief)
        self.assertEqual(code, 1)
        self.assertIn("cannot read --task-file", err)


class InitErrorsAndReplaceTest(InitTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo(branch="feature/first")

    def test_outside_git_repo(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        os.environ["GIT_CEILING_DIRECTORIES"] = self.tmp
        code, out, err = self.init(cwd=plain)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, "tandem: not inside a git repository\n")
        self.assertFalse(os.path.exists(paths.tandem_home()))

    def test_active_session_blocks_second_init(self):
        self.init_ok()
        code, out, err = self.init("second")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("active session repo--first", err)
        self.assertIn("--replace", err)
        self.assertEqual(self.session_dirs(), ["repo--first"])
        self.assertEqual(session.read_pointer(self.repo), "repo--first")

    def test_active_session_blocks_from_subdirectory(self):
        self.init_ok()
        sub = os.path.join(self.repo, "sub")
        os.makedirs(sub)
        self.assertEqual(self.init("second", cwd=sub)[0], 1)

    def test_replace_ends_old_session(self):
        self.init_ok()
        new = self.init_ok("second", "--replace")
        self.assertEqual(new["dir"], "repo--second")
        self.assertIsNone(new["ended"])

        old = session.load_session("repo--first")
        self.assertEqual(old["ended"], "2026-11-11T10:05:00Z")
        self.assertEqual(old["replaced_by"], "repo--second")
        self.assertEqual(session.read_pointer(self.repo), "repo--second")

        old_log = self.log_lines("repo--first")
        self.assertEqual([e["event"] for e in old_log], ["init", "replaced"])
        self.assertEqual(old_log[-1]["replaced_by"], "repo--second")
        new_log = self.log_lines("repo--second")
        self.assertEqual(new_log[0]["event"], "init")
        self.assertEqual(new_log[0]["replaces"], "repo--first")

    def test_replace_without_active_session_just_inits(self):
        meta = self.init_ok("--replace")
        self.assertEqual(meta["dir"], "repo--first")
        self.assertNotIn("replaces", self.log_lines("repo--first")[0])

    def test_ended_session_pointer_does_not_block(self):
        self.init_ok()
        old = session.load_session("repo--first")
        old["ended"] = "2026-11-11T11:00:00Z"
        session.save_session(old)
        meta = self.init_ok("second")
        self.assertEqual(meta["dir"], "repo--second")
        self.assertIsNone(session.load_session("repo--first")["replaced_by"])

    def test_pointer_to_missing_session_does_not_block(self):
        self.init_ok()
        os.rename(session.session_path("repo--first"), os.path.join(self.tmp, "gone"))
        self.assertEqual(self.init_ok("second")["dir"], "repo--second")
        self.assertEqual(session.read_pointer(self.repo), "repo--second")

    def test_worktree_gets_its_own_session(self):
        self.init_ok()
        wt = os.path.join(self.tmp, "repo-wt")
        self.git(self.repo, "worktree", "add", "-q", "-b", "feature/second", wt)
        meta = self.init_ok(cwd=wt)
        self.assertEqual(meta["dir"], "repo-wt--second")
        self.assertEqual(session.read_pointer(self.repo), "repo--first")
        self.assertEqual(session.read_pointer(wt), "repo-wt--second")
