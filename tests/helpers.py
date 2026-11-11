import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tandem import cli, paths

SHIM = paths.shim_path()

# Variables a launched harness exports; a test inheriting them from the
# developer's own tandem session would resolve that session instead of its own.
_CLEARED_ENV = ("TANDEM_SESSION", "TANDEM_ROLE", "TANDEM_HOME")


class TandemTestCase(unittest.TestCase):
    """Each test gets a fresh HOME and TANDEM_HOME under a temp dir.

    HOME is replaced too, so a code path that ignores TANDEM_HOME still can't
    reach the developer's real ~/.tandem.
    """

    def setUp(self):
        super().setUp()
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="tandem-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        saved = dict(os.environ)

        def restore():
            os.environ.clear()
            os.environ.update(saved)

        self.addCleanup(restore)
        for key in _CLEARED_ENV:
            os.environ.pop(key, None)
        # A suite run from an agy pane would otherwise reach the real language server.
        for key in [k for k in os.environ if k.startswith("ANTIGRAVITY_")]:
            del os.environ[key]

        self.home = os.path.join(self.tmp, "home")
        self.tandem_home = os.path.join(self.tmp, "tandem-home")
        os.makedirs(self.home)
        os.environ["HOME"] = self.home
        os.environ["TANDEM_HOME"] = self.tandem_home
        # Keeps the user's git config (signing, hooks, templates) out of fixture repos.
        os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        for who in ("AUTHOR", "COMMITTER"):
            os.environ["GIT_%s_NAME" % who] = "tandem test"
            os.environ["GIT_%s_EMAIL" % who] = "tandem@example.invalid"

        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)

    def git(self, repo, *args):
        return subprocess.run(
            ["git", "-C", repo] + list(args),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        ).stdout.strip()

    def make_repo(self, name="repo", branch="feature/test-branch"):
        """A git repo with one commit, checked out on `branch` (None: stay on main)."""
        repo = os.path.join(self.tmp, name)
        os.makedirs(repo)
        self.git(repo, "init", "-q", "-b", "main")
        with open(os.path.join(repo, "README"), "w") as f:
            f.write("fixture\n")
        self.git(repo, "add", "README")
        self.git(repo, "commit", "-q", "-m", "init")
        if branch:
            self.git(repo, "checkout", "-q", "-b", branch)
        return repo

    def run_cli(self, *argv, stdin=""):
        """Run `tandem.cli.main` in-process; returns (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        saved_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(argv))
        finally:
            sys.stdin = saved_stdin
        return code, out.getvalue(), err.getvalue()

    def run_shim(self, *argv, shim=SHIM, stdin="", cwd=None):
        """Run the shim under the interpreter running the tests, so `make test`
        exercises each interpreter rather than whatever `env python3` finds."""
        return subprocess.run(
            [sys.executable, shim] + list(argv),
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            cwd=cwd,
            env=dict(os.environ),
        )
