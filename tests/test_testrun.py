import fcntl
import json
import os
import signal
import subprocess
import sys

from tandem import session, testrun
from tests.helpers import SHIM, TandemTestCase


class TestRunTestCase(TandemTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("apix")
        self.dir = session.init_session(cwd=self.repo, name="locks", test_cmds=["go test"])["dir"]
        self.sdir = session.session_path(self.dir)
        self.out = os.path.join(self.tmp, "out")

    def py(self, code):
        return [sys.executable, "-c", code]

    def start(self, *argv, role="navigator"):
        env = dict(os.environ, TANDEM_ROLE=role)
        return subprocess.Popen(
            [sys.executable, SHIM, "--session", self.dir, "test"] + list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            env=env,
        )

    def hold_lock(self):
        f = open(testrun.lock_path(self.sdir), "a")
        self.addCleanup(f.close)
        fcntl.flock(f, fcntl.LOCK_EX)
        return f


class RunTest(TestRunTestCase):
    def test_runs_in_the_repo_and_returns_the_exit_code(self):
        code, _, err = self.run_cli(
            "--session", self.dir, "test", *self.py("import os; open(%r, 'w').write(os.getcwd()); raise SystemExit(3)" % self.out)
        )
        self.assertEqual(code, 3, err)
        with open(self.out) as f:
            self.assertEqual(f.read(), self.repo)

    def test_leading_double_dash_is_dropped(self):
        code, _, err = self.run_cli("--session", self.dir, "test", "--", *self.py("pass"))
        self.assertEqual(code, 0, err)

    def test_releases_the_lock_and_holder(self):
        self.run_cli("--session", self.dir, "test", *self.py("pass"))
        self.assertFalse(os.path.exists(testrun.holder_path(self.sdir)))
        with open(testrun.lock_path(self.sdir), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_holder_names_the_run_while_it_runs(self):
        code = "import json; open(%r, 'w').write(open(%r).read())" % (self.out, testrun.holder_path(self.sdir))
        proc = self.start(*self.py(code), role="driver")
        proc.communicate(timeout=30)
        with open(self.out) as f:
            holder = json.load(f)
        self.assertEqual(holder["role"], "driver")
        self.assertEqual(holder["pid"], proc.pid)
        self.assertTrue(holder["command"].startswith(sys.executable + " -c "))

    def test_signal_death_maps_to_shell_code(self):
        code, _, _ = self.run_cli("--session", self.dir, "test", *self.py("import os, signal; os.kill(os.getpid(), signal.SIGTERM)"))
        self.assertEqual(code, 128 + signal.SIGTERM)

    def test_missing_program(self):
        code, _, err = self.run_cli("--session", self.dir, "test", os.path.join(self.tmp, "no-such-tool"))
        self.assertEqual(code, 1)
        self.assertIn("tandem: cannot run", err)
        self.assertFalse(os.path.exists(testrun.holder_path(self.sdir)))

    def test_nothing_to_run(self):
        for argv in ((), ("--",)):
            code, _, err = self.run_cli("--session", self.dir, "test", *argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("nothing to run", err)

    def test_ended_session(self):
        self.mark_ended()
        code, _, err = self.run_cli("--session", self.dir, "test", *self.py("pass"))
        self.assertEqual(code, 1)
        self.assertIn("has ended", err)


class LockTest(TestRunTestCase):
    def test_waits_for_the_other_run_and_says_whose_it_is(self):
        lock = self.hold_lock()
        session.write_json_atomic(
            testrun.holder_path(self.sdir),
            {"role": "driver", "command": "go test ./...", "pid": 4242, "since": "2026-11-23T10:00:00Z"},
        )
        proc = self.start(*self.py("open(%r, 'w').write('ran')" % self.out))
        # Printed just before it blocks on the lock.
        waiting = proc.stderr.readline()
        self.assertEqual(
            waiting,
            "tandem: waiting for the test lock, held by the driver's `go test ./...` (pid 4242, since 2026-11-23T10:00:00Z)\n",
        )
        self.assertIsNone(proc.poll())
        self.assertFalse(os.path.exists(self.out))
        fcntl.flock(lock, fcntl.LOCK_UN)
        _, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        with open(self.out) as f:
            self.assertEqual(f.read(), "ran")

    def test_unreadable_holder_still_waits(self):
        lock = self.hold_lock()
        with open(testrun.holder_path(self.sdir), "w") as f:
            f.write("{not json")
        proc = self.start(*self.py("pass"))
        self.assertIn("held by another test run", proc.stderr.readline())
        fcntl.flock(lock, fcntl.LOCK_UN)
        _, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)

    def test_sigterm_is_forwarded_and_waited_for(self):
        started = os.path.join(self.tmp, "started")
        code = "import time; open(%r, 'w').close(); time.sleep(30)" % started
        proc = self.start(*self.py(code))
        self.wait_for(lambda: os.path.exists(started), timeout=30)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=30)
        # The wrapper exits through its own return, with the child's code; an unforwarded SIGTERM
        # would have killed it outright (-15) and left the child sleeping without the lock.
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM)
        self.assertFalse(os.path.exists(testrun.holder_path(self.sdir)))
