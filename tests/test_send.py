import datetime
import json
import os
import subprocess
import sys
from unittest import mock

from tandem import TandemError, maildir, session
from tests.helpers import SHIM, TandemTestCase

FIXED = datetime.datetime(2026, 11, 11, 10, 5, 7, tzinfo=datetime.timezone.utc)


class SendTestCase(TandemTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("apix")
        self.dir = session.init_session(cwd=self.repo, name="fix-csv-bom")["dir"]
        self.sdir = session.session_path(self.dir)
        os.chdir(self.repo)

    def send(self, *argv, stdin=""):
        return self.run_cli("send", *argv, stdin=stdin)

    def send_ok(self, *argv, stdin=""):
        code, out, err = self.send(*argv, stdin=stdin)
        self.assertEqual(code, 0, err)
        return out

    def send_error(self, *argv, stdin=""):
        code, out, err = self.send(*argv, stdin=stdin)
        self.assertEqual(code, 1, out)
        self.assertEqual(out, "")
        return err

    def inbox(self, role, sub="new"):
        return sorted(os.listdir(maildir.inbox_path(self.sdir, role, sub)))

    def message(self, role, msg_id, sub="new"):
        with open(os.path.join(maildir.inbox_path(self.sdir, role, sub), msg_id + ".json"), encoding="utf-8") as f:
            return json.load(f)

    def messages_logged(self):
        with open(os.path.join(self.sdir, "log.jsonl"), encoding="utf-8") as f:
            records = [json.loads(line) for line in f]
        return [r for r in records if r["kind"] == "message"]

    def end_session(self):
        meta = session.load_session(self.dir)
        meta["ended"] = "2026-11-11T12:00:00Z"
        session.save_session(meta)


class EnvelopeTest(SendTestCase):
    def test_envelope_shape(self):
        with mock.patch("tandem.session._now", return_value=FIXED):
            out = self.send_ok("--as", "driver", "--to", "navigator", "--type", "review_request", "please review")
        self.assertEqual(out, "sent 0001 to navigator\n")
        self.assertEqual(self.inbox("navigator"), ["0001.json"])
        self.assertEqual(self.inbox("navigator", "tmp"), [])
        msg = self.message("navigator", "0001")
        self.assertEqual(
            list(msg), ["id", "ts", "from", "to", "type", "reply_to", "expects_reply", "refs", "body"]
        )
        self.assertEqual(
            msg,
            {
                "id": "0001",
                "ts": "2026-11-11T10:05:07Z",
                "from": "driver",
                "to": "navigator",
                "type": "review_request",
                "reply_to": None,
                "expects_reply": True,
                "refs": {},
                "body": "please review",
            },
        )

    def test_log_line_is_the_envelope_with_kind(self):
        self.send_ok("--as", "driver", "--to", "navigator", "hello")
        logged = self.messages_logged()
        self.assertEqual(len(logged), 1)
        record = dict(logged[0])
        self.assertEqual(record.pop("kind"), "message")
        self.assertEqual(record, self.message("navigator", "0001"))

    def test_ids_increase_across_inboxes(self):
        self.send_ok("--as", "driver", "--to", "navigator", "one")
        self.send_ok("--as", "navigator", "--to", "driver", "two")
        self.send_ok("--as", "driver", "--to", "human", "three")
        self.assertEqual(self.inbox("navigator"), ["0001.json"])
        self.assertEqual(self.inbox("driver"), ["0002.json"])
        self.assertEqual(self.inbox("human"), ["0003.json"])
        self.assertEqual([m["id"] for m in self.messages_logged()], ["0001", "0002", "0003"])

    def test_read_messages_still_count_for_ids(self):
        self.send_ok("--as", "driver", "--to", "navigator", "one")
        os.rename(
            os.path.join(maildir.inbox_path(self.sdir, "navigator", "new"), "0001.json"),
            os.path.join(maildir.inbox_path(self.sdir, "navigator", "cur"), "0001.json"),
        )
        self.assertEqual(self.send_ok("--as", "driver", "--to", "navigator", "two"), "sent 0002 to navigator\n")

    def test_ids_widen_past_four_digits(self):
        cur = maildir.inbox_path(self.sdir, "driver", "cur")
        open(os.path.join(cur, "9999.json"), "w").close()
        self.assertEqual(self.send_ok("--as", "navigator", "--to", "driver", "x"), "sent 10000 to driver\n")

    def test_leftover_tmp_file_is_overwritten(self):
        tmp = maildir.inbox_path(self.sdir, "navigator", "tmp")
        with open(os.path.join(tmp, "0001.json"), "w") as f:
            f.write("partial")
        self.send_ok("--as", "driver", "--to", "navigator", "whole")
        self.assertEqual(self.inbox("navigator", "tmp"), [])
        self.assertEqual(self.message("navigator", "0001")["body"], "whole")

    def test_expects_reply_defaults_per_type(self):
        for n, type_ in enumerate(maildir.TYPES, 1):
            self.send_ok("--as", "driver", "--to", "navigator", "--type", type_, "x")
            expected = type_ in ("plan", "review_request", "question", "escalate")
            self.assertEqual(self.message("navigator", maildir.format_id(n))["expects_reply"], expected, type_)

    def test_expects_reply_overrides(self):
        self.send_ok("--as", "driver", "--to", "navigator", "--type", "question", "--no-reply", "x")
        self.send_ok("--as", "driver", "--to", "navigator", "--type", "decision", "--expects-reply", "x")
        self.assertFalse(self.message("navigator", "0001")["expects_reply"])
        self.assertTrue(self.message("navigator", "0002")["expects_reply"])

    def test_default_type_is_question(self):
        self.send_ok("--as", "driver", "--to", "navigator", "x")
        msg = self.message("navigator", "0001")
        self.assertEqual((msg["type"], msg["expects_reply"]), ("question", True))

    def test_reply_defaults_to_answer(self):
        self.send_ok("--as", "driver", "--to", "navigator", "q")
        self.send_ok("--as", "navigator", "--to", "driver", "--reply-to", "1", "a")
        msg = self.message("driver", "0002")
        self.assertEqual((msg["type"], msg["reply_to"], msg["expects_reply"]), ("answer", "0001", False))

    def test_reply_to_forms_normalize(self):
        self.send_ok("--as", "driver", "--to", "navigator", "q")
        for n, form in enumerate(("0001", "#0001", "1"), 2):
            self.send_ok("--as", "navigator", "--to", "driver", "--reply-to", form, "a")
            self.assertEqual(self.message("driver", maildir.format_id(n))["reply_to"], "0001")

    def test_refs_from_commit_and_files(self):
        head = self.git(self.repo, "rev-parse", "HEAD")
        self.send_ok(
            "--as", "driver", "--to", "navigator", "--commit", "HEAD", "--file", "plan.md", "--file", "a/b.go", "x"
        )
        refs = self.message("navigator", "0001")["refs"]
        self.assertEqual(refs["files"], ["plan.md", "a/b.go"])
        self.assertEqual(len(refs["commit"]), 12)
        self.assertTrue(head.startswith(refs["commit"]))

    def test_commit_resolves_against_session_repo_not_cwd(self):
        other = self.make_repo("other")
        os.chdir(other)
        head = self.git(self.repo, "rev-parse", "HEAD")
        code, _, err = self.run_cli("--session", self.dir, "send", "--as", "driver", "--to", "navigator", "--commit", head[:7], "x")
        self.assertEqual(code, 0, err)
        self.assertTrue(head.startswith(self.message("navigator", "0001")["refs"]["commit"]))

    def test_body_from_stdin(self):
        self.send_ok("--as", "driver", "--to", "navigator", stdin="\n# Plan\n\n- step one\n\n")
        self.assertEqual(self.message("navigator", "0001")["body"], "# Plan\n\n- step one")

    def test_body_keeps_leading_indentation(self):
        self.send_ok("--as", "driver", "--to", "navigator", stdin="    code block\n")
        self.assertEqual(self.message("navigator", "0001")["body"], "    code block")

    def test_argument_beats_stdin(self):
        self.send_ok("--as", "driver", "--to", "navigator", "from arg", stdin="from stdin")
        self.assertEqual(self.message("navigator", "0001")["body"], "from arg")

    def test_unicode_body_stored_verbatim(self):
        self.send_ok("--as", "driver", "--to", "navigator", "naïve → ok")
        with open(os.path.join(maildir.inbox_path(self.sdir, "navigator", "new"), "0001.json"), encoding="utf-8") as f:
            self.assertIn("naïve → ok", f.read())

    def test_as_defaults_to_tandem_role(self):
        os.environ["TANDEM_ROLE"] = "navigator"
        self.send_ok("--to", "driver", "x")
        self.assertEqual(self.message("driver", "0001")["from"], "navigator")

    def test_as_beats_tandem_role(self):
        os.environ["TANDEM_ROLE"] = "navigator"
        self.send_ok("--as", "human", "--to", "driver", "x")
        self.assertEqual(self.message("driver", "0001")["from"], "human")

    def test_tandem_session_env_resolves(self):
        os.chdir(self.tmp)
        os.environ["TANDEM_SESSION"] = self.dir
        self.send_ok("--as", "driver", "--to", "navigator", "x")
        self.assertEqual(self.inbox("navigator"), ["0001.json"])

    def test_through_the_shim(self):
        result = self.run_shim("send", "--as", "driver", "--to", "navigator", stdin="piped\n", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "sent 0001 to navigator\n")
        self.assertEqual(self.message("navigator", "0001")["body"], "piped")


class SendErrorTest(SendTestCase):
    def assert_nothing_sent(self):
        for role in session.ROLES:
            for sub in ("tmp", "new", "cur"):
                self.assertEqual(self.inbox(role, sub), [], (role, sub))
        self.assertEqual(self.messages_logged(), [])

    def test_bad_type(self):
        err = self.send_error("--as", "driver", "--to", "navigator", "--type", "chat", "x")
        self.assertIn("--type must be one of", err)
        self.assert_nothing_sent()

    def test_bad_recipient(self):
        err = self.send_error("--as", "driver", "--to", "reviewer", "x")
        self.assertIn("--to must be one of driver, navigator, human", err)
        self.assert_nothing_sent()

    def test_bad_sender(self):
        self.assertIn("--as must be one of", self.send_error("--as", "claude", "--to", "navigator", "x"))
        self.assert_nothing_sent()

    def test_bad_tandem_role(self):
        os.environ["TANDEM_ROLE"] = "agy"
        self.assertIn("--as must be one of", self.send_error("--to", "navigator", "x"))

    def test_missing_as(self):
        err = self.send_error("--to", "navigator", "x")
        self.assertIn("no sender role", err)
        self.assert_nothing_sent()

    def test_empty_tandem_role_is_missing(self):
        os.environ["TANDEM_ROLE"] = ""
        self.assertIn("no sender role", self.send_error("--to", "navigator", "x"))

    def test_missing_to(self):
        self.assertIn("--to", self.send_error("--as", "driver", "x"))

    def test_send_to_self(self):
        self.assertIn("cannot send to yourself", self.send_error("--as", "driver", "--to", "driver", "x"))
        self.assert_nothing_sent()

    def test_empty_body(self):
        for argv, stdin in ((("",), ""), (("  \n",), ""), ((), ""), ((), "\n\n  \n")):
            err = self.send_error("--as", "driver", "--to", "navigator", *argv, stdin=stdin)
            self.assertIn("message body is empty", err)
        self.assert_nothing_sent()

    def test_terminal_stdin_is_not_read(self):
        class Tty:
            def isatty(self):
                return True

            def read(self):
                raise AssertionError("read a terminal")

        with mock.patch("sys.stdin", Tty()):
            with self.assertRaises(TandemError) as ctx:
                maildir._read_body(mock.Mock(body=None))
        self.assertIn("message body is empty", str(ctx.exception))

    def test_ended_session(self):
        self.end_session()
        os.environ["TANDEM_SESSION"] = self.dir
        err = self.send_error("--as", "driver", "--to", "navigator", "x")
        self.assertIn("has ended", err)
        self.assert_nothing_sent()

    def test_deliver_rechecks_ended_under_the_lock(self):
        meta = session.load_session(self.dir)
        self.end_session()
        envelope = maildir.build_envelope("driver", "navigator", "x")
        with self.assertRaises(TandemError):
            maildir.deliver(meta, envelope)
        self.assert_nothing_sent()

    def test_no_session(self):
        os.chdir(self.make_repo("lonely"))
        self.assertIn("no active session", self.send_error("--as", "driver", "--to", "navigator", "x"))

    def test_reply_to_unknown_message(self):
        err = self.send_error("--as", "driver", "--to", "navigator", "--reply-to", "0042", "x")
        self.assertIn("no such message", err)
        self.assert_nothing_sent()

    def test_reply_to_bad_id(self):
        for bad in ("abc", "0", "-1", "1.5"):
            err = self.send_error("--as", "driver", "--to", "navigator", "--reply-to=" + bad, "x")
            self.assertIn("bad message id", err, bad)
        self.assert_nothing_sent()

    def test_bad_commit(self):
        for bad in ("nope", "-v", "HEAD~99"):
            err = self.send_error("--as", "driver", "--to", "navigator", "--commit=" + bad, "x")
            self.assertIn("not a commit", err, bad)
        self.assert_nothing_sent()

    def test_expects_and_no_reply_conflict(self):
        self.send_error("--as", "driver", "--to", "navigator", "--expects-reply", "--no-reply", "x")
        self.assert_nothing_sent()


class ConcurrentSendTest(SendTestCase):
    PROCS = 8
    EACH = 5

    def test_parallel_senders_get_unique_gap_free_ids(self):
        script = (
            "import sys\n"
            "from tandem import cli\n"
            "for i in range(%d):\n"
            "    code = cli.main(['send', '--as', sys.argv[1], '--to', sys.argv[2], 'p%%s-%%d' %% (sys.argv[3], i)])\n"
            "    if code:\n"
            "        sys.exit(code)\n" % self.EACH
        )
        pairs = [("driver", "navigator"), ("navigator", "driver"), ("human", "driver"), ("driver", "human")]
        procs = []
        for n in range(self.PROCS):
            sender, to = pairs[n % len(pairs)]
            procs.append(
                subprocess.Popen(
                    [sys.executable, "-c", script, sender, to, str(n)],
                    cwd=self.repo,
                    env=dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(SHIM))),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                )
            )
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)

        total = self.PROCS * self.EACH
        expected = [maildir.format_id(n) for n in range(1, total + 1)]
        files = []
        for role in session.ROLES:
            files.extend(name[: -len(".json")] for name in self.inbox(role))
            self.assertEqual(self.inbox(role, "tmp"), [])
        self.assertEqual(sorted(files), expected)
        logged = self.messages_logged()
        self.assertEqual([m["id"] for m in logged], expected)
        self.assertEqual(len({m["body"] for m in logged}), total)
        for m in logged:
            self.assertEqual(self.message(m["to"], m["id"])["body"], m["body"])
