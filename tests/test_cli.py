import os
import subprocess
import sys

from tandem import TandemError, __version__, cli, paths
from tests.helpers import SHIM, TandemTestCase


class ShimTest(TandemTestCase):
    def test_version_through_symlinked_shim(self):
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        link = os.path.join(bindir, "tandem")
        os.symlink(SHIM, link)
        result = self.run_shim("--version", shim=link, cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "tandem %s" % __version__)

    def test_shim_is_executable_with_env_python3_shebang(self):
        self.assertTrue(os.access(SHIM, os.X_OK))
        with open(SHIM) as f:
            self.assertEqual(f.readline().strip(), "#!/usr/bin/env python3")

    def test_python_dash_m(self):
        result = subprocess.run(
            [sys.executable, "-m", "tandem", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            cwd=paths.REPO_ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "tandem %s" % __version__)

    def test_unknown_subcommand_through_shim_exits_1(self):
        result = self.run_shim("no-such-command", cwd=self.tmp)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.startswith("tandem: "), result.stderr)


class CliTest(TandemTestCase):
    def setUp(self):
        super().setUp()
        saved = list(cli.COMMANDS)

        def restore():
            cli.COMMANDS[:] = saved

        self.addCleanup(restore)

    def test_unknown_subcommand_exits_1(self):
        code, out, err = self.run_cli("no-such-command")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("tandem: "), err)

    def test_missing_subcommand_exits_1(self):
        code, _, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("tandem: "), err)

    def test_bad_option_on_subcommand_exits_1(self):
        cli.register("probe", "test command", lambda p: None, lambda a: 0)
        code, _, err = self.run_cli("probe", "--bogus")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("tandem: "), err)

    def test_help_exits_0(self):
        code, out, _ = self.run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("--session", out)

    def test_registered_command_gets_global_session(self):
        seen = {}

        def configure(p):
            p.add_argument("--flag", action="store_true")

        def run(args):
            seen["session"] = args.session
            seen["flag"] = args.flag
            return 3

        cli.register("probe", "test command", configure, run)
        code, _, _ = self.run_cli("--session", "repo--x", "probe", "--flag")
        self.assertEqual(code, 3)
        self.assertEqual(seen, {"session": "repo--x", "flag": True})

    def test_command_returning_none_exits_0(self):
        cli.register("probe", "test command", lambda p: None, lambda a: None)
        self.assertEqual(self.run_cli("probe")[0], 0)

    def test_tandem_error_prints_prefixed_message_and_exits_1(self):
        def run(args):
            raise TandemError("no active session")

        cli.register("probe", "test command", lambda p: None, run)
        code, out, err = self.run_cli("probe")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, "tandem: no active session\n")

    def test_duplicate_registration_rejected(self):
        cli.register("probe", "test command", lambda p: None, lambda a: 0)
        with self.assertRaises(ValueError):
            cli.register("probe", "again", lambda p: None, lambda a: 0)


class PathsTest(TandemTestCase):
    def test_tandem_home_override(self):
        self.assertEqual(paths.tandem_home(), self.tandem_home)

    def test_tandem_home_relative_override_made_absolute(self):
        os.chdir(self.tmp)
        os.environ["TANDEM_HOME"] = "rel-home"
        self.assertEqual(paths.tandem_home(), os.path.join(self.tmp, "rel-home"))

    def test_tandem_home_default_under_home(self):
        del os.environ["TANDEM_HOME"]
        self.assertEqual(paths.tandem_home(), os.path.join(self.home, ".tandem"))

    def test_empty_tandem_home_falls_back_to_default(self):
        os.environ["TANDEM_HOME"] = ""
        self.assertEqual(paths.tandem_home(), os.path.join(self.home, ".tandem"))

    def test_shim_path_is_absolute_checkout_shim(self):
        shim = paths.shim_path()
        self.assertTrue(os.path.isabs(shim))
        self.assertTrue(os.path.isfile(shim))
        self.assertEqual(shim, os.path.join(paths.REPO_ROOT, "bin", "tandem"))


class FixtureTest(TandemTestCase):
    def test_env_isolation(self):
        self.assertNotIn("TANDEM_SESSION", os.environ)
        self.assertNotIn("TANDEM_ROLE", os.environ)
        self.assertTrue(paths.tandem_home().startswith(self.tmp))
        self.assertEqual(os.path.expanduser("~"), self.home)

    def test_make_repo_on_branch(self):
        repo = self.make_repo(branch="feature/abc")
        self.assertEqual(self.git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "feature/abc")
        self.assertEqual(self.git(repo, "rev-parse", "--show-toplevel"), repo)

    def test_make_repo_on_main(self):
        repo = self.make_repo(branch=None)
        self.assertEqual(self.git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
