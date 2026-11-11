import json
import os
import subprocess
import sys
import threading
import time
from unittest import mock

from tandem import maildir, session
from tests.helpers import SHIM, TandemTestCase


class RecvTestCase(TandemTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("apix")
        self.dir = session.init_session(cwd=self.repo, name="fix-csv-bom")["dir"]
        self.sdir = session.session_path(self.dir)
        self.meta = session.load_session(self.dir)
        os.chdir(self.repo)
        patcher = mock.patch("tandem.maildir.POLL_INTERVAL", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)

    def deliver(self, body="hello", sender="driver", to="navigator", **kw):
        return maildir.deliver(self.meta, maildir.build_envelope(sender, to, body, **kw))

    def recv(self, *argv):
        return self.run_cli("recv", *argv)

    def recv_ok(self, *argv):
        code, out, err = self.recv(*argv)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        return out

    def recv_none(self, *argv):
        code, out, err = self.recv(*argv)
        self.assertEqual(code, 3, err)
        self.assertEqual(out, "")
        return err

    def recv_error(self, *argv):
        code, out, err = self.recv(*argv)
        self.assertEqual(code, 1, out)
        self.assertEqual(out, "")
        return err

    def inbox(self, role, sub="new"):
        return sorted(os.listdir(maildir.inbox_path(self.sdir, role, sub)))

    def write_raw(self, role, name, text):
        with open(os.path.join(maildir.inbox_path(self.sdir, role, "new"), name), "w", encoding="utf-8") as f:
            f.write(text)

    def drop_raw_message(self, role):
        """A message file put straight into `new/`, as if delivered, even into an ended session."""
        path = os.path.join(maildir.inbox_path(self.sdir, role, "new"), "9999.json")
        with open(path, "w") as f:
            json.dump({"id": "9999", "from": "driver", "to": role, "type": "question", "body": "guard"}, f)


class OrderingTest(RecvTestCase):
    def test_pops_lowest_id_first(self):
        for body in ("one", "two", "three"):
            self.deliver(body)
        bodies = [json.loads(self.recv_ok("--as", "navigator", "--json"))["body"] for _ in range(3)]
        self.assertEqual(bodies, ["one", "two", "three"])
        self.recv_none("--as", "navigator")

    def test_other_inboxes_untouched(self):
        self.deliver("to driver", sender="navigator", to="driver")
        self.deliver("to navigator")
        self.assertEqual(json.loads(self.recv_ok("--as", "navigator", "--json"))["id"], "0002")
        self.recv_none("--as", "navigator")
        self.assertEqual(self.inbox("driver"), ["0001.json"])

    def test_ids_sort_numerically_past_9999(self):
        for n in (10000, 9999, 10001):
            msg = {"id": maildir.format_id(n), "from": "driver", "to": "navigator", "type": "answer",
                   "reply_to": None, "expects_reply": False, "refs": {}, "body": str(n)}
            self.write_raw("navigator", msg["id"] + ".json", json.dumps(msg))
        ids = [json.loads(self.recv_ok("--as", "navigator", "--json"))["id"] for _ in range(3)]
        self.assertEqual(ids, ["9999", "10000", "10001"])

    def test_ignores_non_message_files(self):
        self.write_raw("navigator", ".DS_Store", "junk")
        self.write_raw("navigator", "7.json", "{}")
        self.deliver("real")
        self.assertEqual(json.loads(self.recv_ok("--as", "navigator", "--json"))["body"], "real")
        self.recv_none("--as", "navigator")

    def test_lost_race_moves_on_to_next_file(self):
        self.deliver("taken by another reader")
        self.deliver("ours")
        real_rename = os.rename
        calls = []

        def rename(src, dst):
            calls.append(src)
            if len(calls) == 1:
                raise FileNotFoundError(src)
            return real_rename(src, dst)

        with mock.patch("tandem.maildir.os.rename", side_effect=rename):
            msg = maildir.pop(self.sdir, "navigator")
        self.assertEqual(msg["body"], "ours")
        self.assertEqual([os.path.basename(c) for c in calls], ["0001.json", "0002.json"])

    def test_unreadable_message_is_set_aside(self):
        self.write_raw("navigator", "0001.json", "{not json")
        self.deliver("next")
        err = self.recv_error("--as", "navigator")
        self.assertIn("unreadable message", err)
        self.assertIn("set aside as read; read again for the next message", err)
        self.assertEqual(self.inbox("navigator", "cur"), ["0001.json"])
        self.assertEqual(json.loads(self.recv_ok("--as", "navigator", "--json"))["body"], "next")


class PeekPopTest(RecvTestCase):
    def test_pop_moves_to_cur(self):
        self.deliver()
        self.recv_ok("--as", "navigator")
        self.assertEqual(self.inbox("navigator"), [])
        self.assertEqual(self.inbox("navigator", "cur"), ["0001.json"])

    def test_peek_leaves_message_in_new(self):
        self.deliver("first")
        self.deliver("second")
        first = self.recv_ok("--as", "navigator", "--peek")
        self.assertEqual(self.recv_ok("--as", "navigator", "--peek"), first)
        self.assertEqual(self.inbox("navigator"), ["0001.json", "0002.json"])
        self.assertEqual(self.inbox("navigator", "cur"), [])
        self.assertEqual(self.recv_ok("--as", "navigator"), first)
        self.assertEqual(self.inbox("navigator"), ["0002.json"])

    def test_peek_empty(self):
        self.assertEqual(self.recv_none("--as", "navigator", "--peek"), "no mail for navigator\n")

    def test_ended_session_still_reads_leftover_mail(self):
        self.deliver("left over")
        self.mark_ended()
        code, out, err = self.run_cli("--session", self.dir, "recv", "--as", "navigator")
        self.assertEqual(code, 0, err)
        self.assertIn("left over", out)


class OutputTest(RecvTestCase):
    def test_json_is_raw_envelope_on_one_line(self):
        sent = self.deliver("line one\nline two", type="review_request", files=["plan.md"])
        out = self.recv_ok("--as", "navigator", "--json")
        self.assertEqual(out.count("\n"), 1)
        self.assertEqual(json.loads(out), sent)
        with open(os.path.join(maildir.inbox_path(self.sdir, "navigator", "cur"), "0001.json"), encoding="utf-8") as f:
            self.assertEqual(json.loads(out), json.load(f))

    def test_json_keeps_unicode(self):
        self.deliver("café ✓")
        self.assertIn("café ✓", self.recv_ok("--as", "navigator", "--json"))

    def test_header_reply_and_expects_reply(self):
        self.deliver("plan", sender="navigator", to="driver", type="plan")
        self.deliver("please look", type="review_request", reply_to="1")
        self.assertEqual(
            self.recv_ok("--as", "navigator"),
            "#0002 driver → navigator  review_request  reply to #0001, expects reply\n\nplease look\n",
        )

    def test_header_plain(self):
        self.deliver("ok", type="answer")
        self.assertEqual(self.recv_ok("--as", "navigator"), "#0001 driver → navigator  answer\n\nok\n")

    def test_header_expects_reply_only(self):
        self.deliver("why?")
        self.assertEqual(self.recv_ok("--as", "navigator"), "#0001 driver → navigator  question  expects reply\n\nwhy?\n")

    def test_header_reply_only(self):
        self.deliver("q", sender="navigator", to="driver")
        self.deliver("a", reply_to="1")
        self.assertEqual(self.recv_ok("--as", "navigator"), "#0002 driver → navigator  answer  reply to #0001\n\na\n")

    def test_refs_rendered(self):
        msg = {"id": "0003", "from": "navigator", "to": "driver", "type": "review", "reply_to": None,
               "expects_reply": False, "refs": {"commit": "abc123def456", "files": ["plan.md", "a b.py"]},
               "body": "looks fine\n\n- nit"}
        self.assertEqual(
            maildir.render(msg),
            "#0003 navigator → driver  review\ncommit: abc123def456\nfiles: plan.md, a b.py\n\nlooks fine\n\n- nit",
        )

    def test_render_from_cli_with_refs(self):
        sha = self.git(self.repo, "rev-parse", "--short=12", "HEAD")
        self.deliver("see", commit=sha, files=["README"], type="decision")
        self.assertEqual(
            self.recv_ok("--as", "navigator"),
            "#0001 driver → navigator  decision\ncommit: %s\nfiles: README\n\nsee\n" % sha,
        )


class RoleTest(RecvTestCase):
    def test_tandem_role_default(self):
        self.deliver("for navigator")
        os.environ["TANDEM_ROLE"] = "navigator"
        self.assertIn("for navigator", self.recv_ok())

    def test_as_beats_tandem_role(self):
        self.deliver("for driver", sender="navigator", to="driver")
        os.environ["TANDEM_ROLE"] = "navigator"
        self.assertIn("for driver", self.recv_ok("--as", "driver"))

    def test_human_inbox(self):
        self.deliver("help", to="human", type="escalate")
        self.assertIn("driver → human  escalate", self.recv_ok("--as", "human"))

    def test_missing_role(self):
        self.assertIn("no reader role", self.recv_error())

    def test_bad_role(self):
        self.assertIn("--as must be one of", self.recv_error("--as", "claude"))
        os.environ["TANDEM_ROLE"] = "agy"
        self.assertIn("--as must be one of", self.recv_error())

    def test_no_session(self):
        self.mark_ended()
        os.chdir(self.tmp)
        self.assertIn("no active", self.recv_error("--as", "navigator").lower())

    def test_missing_inbox(self):
        os.rmdir(maildir.inbox_path(self.sdir, "navigator", "new"))
        self.assertIn("has no navigator inbox", self.recv_error("--as", "navigator"))


class EmptyAndWaitTest(RecvTestCase):
    def test_empty_inbox_exit_3(self):
        self.assertEqual(self.recv_none("--as", "navigator"), "no mail for navigator\n")

    def test_empty_inbox_json_keeps_stdout_empty(self):
        self.recv_none("--as", "navigator", "--json")

    def test_timeout(self):
        start = time.monotonic()
        self.recv_none("--as", "navigator", "--wait", "--timeout", "0.3")
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertLess(elapsed, 3)

    def test_timeout_implies_wait(self):
        start = time.monotonic()
        self.recv_none("--as", "navigator", "--timeout", "0.2")
        self.assertGreaterEqual(time.monotonic() - start, 0.2)

    def test_timeout_zero_checks_once(self):
        self.recv_none("--as", "navigator", "--wait", "--timeout", "0")
        self.deliver("there")
        self.assertIn("there", self.recv_ok("--as", "navigator", "--wait", "--timeout", "0"))

    def test_wait_returns_mail_that_arrives_later(self):
        timer = threading.Timer(0.3, self.deliver, args=("late",))
        timer.start()
        self.addCleanup(timer.cancel)
        start = time.monotonic()
        out = self.recv_ok("--as", "navigator", "--wait", "--timeout", "10")
        self.assertIn("late", out)
        self.assertLess(time.monotonic() - start, 5)

    def test_wait_peek_does_not_consume(self):
        timer = threading.Timer(0.2, self.deliver, args=("late",))
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertIn("late", self.recv_ok("--as", "navigator", "--wait", "--peek", "--timeout", "10"))
        self.assertEqual(self.inbox("navigator"), ["0001.json"])

    def test_wait_on_ended_session_gives_up(self):
        self.mark_ended()
        start = time.monotonic()
        code, out, err = self.run_cli("--session", self.dir, "recv", "--as", "navigator", "--wait")
        self.assertEqual((code, out, err), (3, "", "no mail for navigator\n"))
        self.assertLess(time.monotonic() - start, 2)

    def test_wait_stops_when_session_json_turns_unreadable(self):
        def corrupt():
            with open(os.path.join(self.sdir, "session.json"), "w") as f:
                f.write("{")

        timer = threading.Timer(0.3, corrupt)
        timer.start()
        self.addCleanup(timer.cancel)
        guard = threading.Timer(10, self.drop_raw_message, ("navigator",))
        guard.start()
        self.addCleanup(guard.cancel)
        start = time.monotonic()
        self.assertIsNone(maildir.receive(self.meta, "navigator", wait=True))
        self.assertLess(time.monotonic() - start, 5)

    def test_wait_stops_when_session_ends(self):
        timer = threading.Timer(0.3, self.mark_ended)
        timer.start()
        self.addCleanup(timer.cancel)
        # Safety bound: a wait that missed the end picks this file up and fails the test.
        guard = threading.Timer(10, self.drop_raw_message, ("navigator",))
        guard.start()
        self.addCleanup(guard.cancel)
        start = time.monotonic()
        self.recv_none("--as", "navigator", "--wait")
        self.assertLess(time.monotonic() - start, 5)

    def test_bad_timeout(self):
        self.assertIn("--timeout must be a number", self.recv_error("--as", "navigator", "--timeout", "soon"))
        self.assertIn("must not be negative", self.recv_error("--as", "navigator", "--timeout", "-1"))
        self.assertIn("must not be negative", self.recv_error("--as", "navigator", "--timeout", "nan"))


class ProcessTest(RecvTestCase):
    def spawn(self, n):
        env = dict(os.environ, TANDEM_SESSION=self.dir, TANDEM_ROLE="navigator")
        procs = [
            subprocess.Popen([sys.executable, SHIM, "recv", "--json"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, universal_newlines=True, env=env)
            for _ in range(n)
        ]
        results = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            results.append((p.returncode, out, err))
        return results

    def test_shim_recv(self):
        self.deliver("via shim")
        result = self.run_shim("--session", self.dir, "recv", "--as", "navigator")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "#0001 driver → navigator  question  expects reply\n\nvia shim\n")
        result = self.run_shim("--session", self.dir, "recv", "--as", "navigator")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "", "no mail for navigator\n"))

    def test_two_processes_one_message(self):
        for _ in range(3):
            sent = self.deliver("contested")
            results = self.spawn(2)
            codes = sorted(code for code, _, _ in results)
            self.assertEqual(codes, [0, 3], results)
            winner = [out for code, out, _ in results if code == 0][0]
            self.assertEqual(json.loads(winner)["id"], sent["id"])
            self.assertEqual(self.inbox("navigator"), [])

    def test_each_message_popped_exactly_once(self):
        ids = [self.deliver("m%d" % i)["id"] for i in range(3)]
        results = self.spawn(6)
        popped = sorted(json.loads(out)["id"] for code, out, _ in results if code == 0)
        self.assertEqual(popped, ids, results)
        self.assertEqual(sorted(code for code, _, _ in results), [0, 0, 0, 3, 3, 3])
        self.assertEqual(self.inbox("navigator", "cur"), [i + ".json" for i in ids])
