import json
import os
import signal
import subprocess
import sys
import threading
import time
from unittest import mock

from tandem import maildir, session, watch
from tests.helpers import SHIM, TandemTestCase

FAST = 0.01


class WatchTestCase(TandemTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("apix")
        self.meta = session.init_session(cwd=self.repo, name="fix-csv-bom")
        self.dir = self.meta["dir"]
        self.sdir = session.session_path(self.dir)
        os.chdir(self.repo)
        patcher = mock.patch.object(watch, "POLL_INTERVAL", FAST)
        patcher.start()
        self.addCleanup(patcher.stop)

    def deliver(self, to="driver", sender="navigator", body="hello"):
        return maildir.deliver(self.meta, maildir.build_envelope(sender, to, body))

    def later(self, fn, delay=0.1):
        timer = threading.Timer(delay, fn)
        timer.start()
        self.addCleanup(timer.cancel)
        return timer

    def watch(self, *extra, role="driver"):
        return self.run_cli("--session", self.dir, "watch", "--as", role, "--once", *extra)

    def pidfile(self, role="driver"):
        return session.watcher_pidfile(self.sdir, role)

    def write_pidfile(self, pid, role="driver"):
        os.makedirs(os.path.dirname(self.pidfile(role)), exist_ok=True)
        session.write_text_atomic(self.pidfile(role), json.dumps({"pid": pid, "ls_address": None}) + "\n")

    def owner(self, role="driver"):
        return (session.read_watcher(self.sdir, role) or {}).get("pid")

    def mark_ended(self):
        meta = session.load_session(self.dir)
        meta["ended"] = "2026-11-11T12:00:00Z"
        session.save_session(meta)

    def wait_for(self, cond, timeout=5.0):
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                self.fail("condition not met within %ss" % timeout)
            time.sleep(FAST)


class RingTest(WatchTestCase):
    def test_ring_exits_2_with_doorbell_on_stderr(self):
        self.deliver()
        code, out, err = self.watch()
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err, "tandem doorbell: new mail for driver. Run `tandem recv`.\n")
        self.assertEqual(watch.read_rung(self.sdir, "driver"), 1)
        with open(watch.rung_path(self.sdir, "driver")) as f:
            self.assertEqual(f.read(), "0001\n")

    def test_doorbell_carries_no_content(self):
        self.deliver(body="SECRET body text")
        code, _, err = self.watch()
        self.assertEqual(code, 2)
        self.assertNotIn("SECRET", err)
        self.assertNotIn("0001", err)

    def test_mail_arriving_while_waiting_rings(self):
        self.later(self.deliver)
        start = time.monotonic()
        code, _, err = self.watch("--deadline", "5")
        self.assertEqual(code, 2)
        self.assertIn("new mail for driver", err)
        self.assertLess(time.monotonic() - start, 2)

    def test_rung_message_does_not_ring_again(self):
        self.deliver()
        self.assertEqual(self.watch()[0], 2)
        code, out, err = self.watch("--deadline", "0.05")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(watch.read_rung(self.sdir, "driver"), 1)

    def test_newer_message_rings_again(self):
        self.deliver()
        self.assertEqual(self.watch()[0], 2)
        self.later(lambda: self.deliver(body="second"))
        code, _, err = self.watch("--deadline", "5")
        self.assertEqual(code, 2)
        self.assertIn("new mail for driver", err)
        self.assertEqual(watch.read_rung(self.sdir, "driver"), 2)

    def test_batch_rings_once_for_the_highest_id(self):
        for _ in range(3):
            self.deliver()
        self.assertEqual(self.watch()[0], 2)
        self.assertEqual(watch.read_rung(self.sdir, "driver"), 3)
        self.assertEqual(self.watch("--deadline", "0")[0], 0)

    def test_read_mail_does_not_ring(self):
        self.deliver()
        self.assertIsNotNone(maildir.pop(self.sdir, "driver"))
        self.assertEqual(self.watch("--deadline", "0")[0], 0)
        self.assertEqual(watch.read_rung(self.sdir, "driver"), 0)

    def test_other_inboxes_do_not_ring(self):
        self.deliver(to="navigator", sender="driver")
        self.deliver(to="human", sender="driver")
        self.assertEqual(self.watch("--deadline", "0")[0], 0)
        self.assertEqual(self.watch("--deadline", "0", role="navigator")[0], 2)

    def test_role_from_env(self):
        self.deliver(to="navigator", sender="driver")
        os.environ["TANDEM_ROLE"] = "navigator"
        code, _, err = self.run_cli("--session", self.dir, "watch", "--once")
        self.assertEqual(code, 2)
        self.assertIn("new mail for navigator", err)

    def test_shim_run(self):
        self.deliver()
        proc = self.run_shim("--session", self.dir, "watch", "--as", "driver", "--once", "--deadline", "5")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stderr, "tandem doorbell: new mail for driver. Run `tandem recv`.\n")


class RungMarkerTest(WatchTestCase):
    def test_missing_or_garbage_marker_is_zero(self):
        self.assertEqual(watch.read_rung(self.sdir, "driver"), 0)
        os.makedirs(session.peer_dir(self.sdir, "driver"))
        with open(watch.rung_path(self.sdir, "driver"), "w") as f:
            f.write("garbage\n")
        self.assertEqual(watch.read_rung(self.sdir, "driver"), 0)

    def test_marker_never_moves_back(self):
        watch.mark_rung(self.sdir, "driver", 5)
        watch.mark_rung(self.sdir, "driver", 3)
        self.assertEqual(watch.read_rung(self.sdir, "driver"), 5)

    def test_unrung_id_sorts_numerically(self):
        new_dir = maildir.inbox_path(self.sdir, "driver", "new")
        for name in ("9999.json", "10000.json", "notes.txt"):
            open(os.path.join(new_dir, name), "w").close()
        self.assertEqual(watch.unrung_id(self.sdir, "driver"), 10000)
        watch.mark_rung(self.sdir, "driver", 9999)
        self.assertEqual(watch.unrung_id(self.sdir, "driver"), 10000)
        watch.mark_rung(self.sdir, "driver", 10000)
        self.assertIsNone(watch.unrung_id(self.sdir, "driver"))


class ExitZeroTest(WatchTestCase):
    def test_deadline_exits_0(self):
        start = time.monotonic()
        code, out, err = self.watch("--deadline", "0.1")
        elapsed = time.monotonic() - start
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertGreaterEqual(elapsed, 0.1)
        self.assertLess(elapsed, 1)
        self.assertFalse(os.path.exists(self.pidfile()))

    def test_deadline_does_not_overshoot_the_poll_interval(self):
        with mock.patch.object(watch, "POLL_INTERVAL", 10):
            start = time.monotonic()
            self.assertEqual(self.watch("--deadline", "0.1")[0], 0)
            self.assertLess(time.monotonic() - start, 1)

    def test_supersede_exits_0_and_keeps_the_newer_pidfile(self):
        self.later(lambda: self.write_pidfile(4242))
        start = time.monotonic()
        code, out, err = self.watch("--deadline", "5")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(self.owner(), 4242)

    def test_superseded_waiter_leaves_the_ring_to_its_successor(self):
        def supersede_then_mail():
            self.write_pidfile(4242)
            self.deliver()

        self.later(supersede_then_mail)
        self.assertEqual(self.watch("--deadline", "5")[0], 0)
        self.assertEqual(watch.read_rung(self.sdir, "driver"), 0)

    def test_superseded_between_the_check_and_the_lock(self):
        # The successor takes the pidfile after the loop's own check but before the ring is
        # claimed under the lock; the re-check there must leave the ring to it.
        self.deliver()
        answers = iter([False, True, True])
        with mock.patch.object(watch, "superseded", side_effect=lambda *a: next(answers)):
            self.assertIsNone(watch.wait_once(self.meta, "driver", deadline=5, interval=FAST, pid=1001))
        self.assertEqual(watch.read_rung(self.sdir, "driver"), 0)

    def test_newer_waiter_supersedes_older_one(self):
        results = {}
        older = threading.Thread(
            target=lambda: results.setdefault("older", watch.wait_once(self.meta, "driver", deadline=5, interval=FAST, pid=1001))
        )
        older.start()
        self.wait_for(lambda: watch.owns_pidfile(self.sdir, "driver", 1001))
        newer = threading.Thread(
            target=lambda: results.setdefault("newer", watch.wait_once(self.meta, "driver", deadline=5, interval=FAST, pid=1002))
        )
        newer.start()
        older.join(5)
        self.assertFalse(older.is_alive())
        self.assertIsNone(results["older"])
        self.deliver()
        newer.join(5)
        self.assertEqual(results["newer"], watch.doorbell("driver"))
        self.assertFalse(os.path.exists(self.pidfile()))

    def test_ended_session_exits_0(self):
        self.later(self.mark_ended)
        start = time.monotonic()
        code, out, err = self.watch("--deadline", "5")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertLess(time.monotonic() - start, 2)
        self.assertFalse(os.path.exists(self.pidfile()))

    def test_already_ended_session_exits_0_without_a_pidfile(self):
        self.deliver()
        self.mark_ended()
        self.assertEqual(self.watch(), (0, "", ""))
        self.assertFalse(os.path.exists(self.pidfile()))
        self.assertEqual(watch.read_rung(self.sdir, "driver"), 0)

    def test_done_ends_the_wait(self):
        self.later(lambda: self.run_cli("--session", self.dir, "done"))
        self.assertEqual(self.watch("--deadline", "5")[0], 0)

    def test_removed_session_ends_the_wait(self):
        sessions = os.path.dirname(self.sdir)
        self.later(lambda: os.rename(self.sdir, os.path.join(sessions, "gone")))
        self.assertEqual(self.watch("--deadline", "5")[0], 0)

    def test_unreadable_session_ends_the_wait(self):
        def corrupt():
            with open(os.path.join(self.sdir, "session.json"), "w") as f:
                f.write("{")

        self.later(corrupt)
        start = time.monotonic()
        self.assertEqual(self.watch("--deadline", "5"), (0, "", ""))
        self.assertLess(time.monotonic() - start, 2)
        self.assertFalse(os.path.exists(self.pidfile()))

    def test_without_a_deadline_it_waits_for_mail(self):
        self.later(self.deliver, delay=0.2)
        # Safety bound: if the mail is missed, ending the session makes the wait return (and fail).
        self.later(self.mark_ended, delay=10)
        self.assertEqual(self.watch(), (2, "", watch.doorbell("driver") + "\n"))


class PidfileTest(WatchTestCase):
    def test_pidfile_names_the_waiter_while_it_waits(self):
        proc = subprocess.Popen(
            [sys.executable, SHIM, "--session", self.dir, "watch", "--as", "driver", "--once", "--deadline", "30"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.addCleanup(proc.kill)
        self.wait_for(lambda: self.owner() == proc.pid)
        with open(self.pidfile()) as f:
            self.assertEqual(json.load(f), {"pid": proc.pid, "ls_address": None})
        code, out, _ = self.run_cli("status")
        self.assertIn("driver running (pid %d)" % proc.pid, out)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=5)
        self.assertEqual((proc.returncode, out, err), (0, "", ""))
        self.assertFalse(os.path.exists(self.pidfile()))

    def test_release_leaves_a_foreign_pidfile(self):
        self.write_pidfile(4242)
        watch.release_pidfile(self.sdir, "driver", 1001)
        self.assertEqual(self.owner(), 4242)
        watch.release_pidfile(self.sdir, "driver", 4242)
        self.assertFalse(os.path.exists(self.pidfile()))

    def test_missing_or_unreadable_pidfile_does_not_supersede(self):
        self.assertFalse(watch.superseded(self.sdir, "driver", 1001))
        self.write_pidfile(1001)
        with open(self.pidfile(), "w") as f:
            f.write("garbage")
        self.assertFalse(watch.superseded(self.sdir, "driver", 1001))


class ErrorTest(WatchTestCase):
    def error(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 1, err)
        self.assertEqual(out, "")
        return err

    def test_human_has_no_watcher(self):
        self.assertIn("driver, navigator", self.error("--session", self.dir, "watch", "--as", "human", "--once"))

    def test_unknown_role(self):
        self.assertIn("--as", self.error("--session", self.dir, "watch", "--as", "boss", "--once"))

    def test_missing_role(self):
        self.assertIn("no watcher role", self.error("--session", self.dir, "watch", "--once"))

    def test_mode_required(self):
        self.assertIn("--once", self.error("--session", self.dir, "watch", "--as", "driver"))

    def test_bad_deadlines(self):
        for value in ("soon", "-1", "nan"):
            self.assertIn("--deadline", self.error("--session", self.dir, "watch", "--as", "driver", "--once", "--deadline", value))
        self.assertFalse(os.path.exists(self.pidfile()))

    def test_unknown_session(self):
        self.assertIn("nope", self.error("--session", "nope", "watch", "--as", "driver", "--once"))
