import io
import os
import threading

from tandem import TandemError, human, maildir, session
from tests.helpers import TandemTestCase


class FakeSource:
    """Scripted input: each poll returns the next batch; past the end, EOF (or `then`)."""

    def __init__(self, *batches, then=None):
        self.batches = list(batches)
        self.timeouts = []
        self.then = then

    def poll(self, timeout):
        self.timeouts.append(timeout)
        if self.batches:
            batch = self.batches.pop(0)
            if isinstance(batch, BaseException):
                raise batch
            if callable(batch):
                return batch()
            return batch
        if self.then is not None:
            raise self.then
        return None


class HumanTestCase(TandemTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("apix")
        self.meta = session.init_session(cwd=self.repo, name="fix-csv-bom")
        self.dir = self.meta["dir"]
        self.sdir = session.session_path(self.dir)
        os.chdir(self.repo)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def deliver(self, body, sender="driver", to="human", **kw):
        return maildir.deliver(self.meta, maildir.build_envelope(sender, to, body, **kw))

    def repl(self, *batches):
        self.source = FakeSource(*batches)
        return human.Repl(self.meta, self.source, self.out, self.err)

    def inbox(self, role, sub="new"):
        sdir = self.sdir
        names = maildir._inbox_names(sdir, role, sub)
        return [maildir._read_message(os.path.join(maildir.inbox_path(sdir, role, sub), n)) for n in names]

    def end(self, **extra):
        meta = session.load_session(self.dir)
        meta["ended"] = "2026-11-11T12:00:00Z"
        meta.update(extra)
        session.save_session(meta)


class ParseLineTest(TandemTestCase):
    def parse(self, line):
        return human.parse_line(line)

    def bad(self, line):
        with self.assertRaises(TandemError) as cm:
            human.parse_line(line)
        return str(cm.exception)

    def test_bare_text_goes_to_driver(self):
        self.assertEqual(self.parse("  looks good, go ahead \n"), ("send", "driver", None, "looks good, go ahead"))

    def test_addressed(self):
        self.assertEqual(self.parse("@driver use the v2 API"), ("send", "driver", None, "use the v2 API"))
        self.assertEqual(self.parse("@navigator\tcheck the tests"), ("send", "navigator", None, "check the tests"))

    def test_role_is_case_insensitive(self):
        self.assertEqual(self.parse("@Navigator hi").to, "navigator")

    def test_explicit_type(self):
        self.assertEqual(self.parse("@navigator:question why?"), ("send", "navigator", "question", "why?"))
        self.assertEqual(self.parse("@driver:Decision ship it"), ("send", "driver", "decision", "ship it"))

    def test_every_type_parses(self):
        for t in maildir.TYPES:
            self.assertEqual(self.parse("@driver:%s x" % t).type, t)

    def test_body_keeps_inner_text(self):
        self.assertEqual(self.parse("@driver a @navigator b: /log").body, "a @navigator b: /log")

    def test_text_mentioning_a_role_later_is_bare(self):
        self.assertEqual(self.parse("ask @navigator first"), ("send", "driver", None, "ask @navigator first"))

    def test_empty(self):
        for line in ("", "   ", "\n"):
            self.assertEqual(self.parse(line).kind, "empty")

    def test_slash_commands(self):
        for word, kind in (("/log", "log"), ("/status", "status"), ("/quit", "quit"), ("/help", "help"), ("  /QUIT ", "quit")):
            self.assertEqual(self.parse(word), (kind, None, None, None))

    def test_unknown_slash_command(self):
        self.assertIn("unknown command /exit", self.bad("/exit"))
        self.assertIn("unknown command /", self.bad("/"))

    def test_slash_command_with_arguments(self):
        self.assertIn("/log takes no arguments", self.bad("/log -f"))

    def test_unknown_role_is_not_sent_to_driver(self):
        self.assertIn("unknown role @drvier", self.bad("@drvier hello"))
        self.assertIn("unknown role @", self.bad("@ hello"))
        self.assertIn("unknown role @driverhello", self.bad("@driverhello"))

    def test_human_is_not_a_target(self):
        self.assertIn("cannot send to yourself", self.bad("@human note to self"))

    def test_bad_type(self):
        self.assertIn("unknown type 'ok'", self.bad("@driver:ok fine"))
        self.assertIn("unknown type ''", self.bad("@driver: fine"))
        self.assertIn("unknown type 'plan:x'", self.bad("@driver:plan:x fine"))

    def test_missing_body(self):
        self.assertIn("nothing to send to driver", self.bad("@driver"))
        self.assertIn("nothing to send to navigator", self.bad("@navigator:question   "))


class ReplyChoiceTest(HumanTestCase):
    def read_all(self):
        while maildir.pop(self.sdir, "human"):
            pass

    def choose(self, line):
        return human.choose_reply(self.sdir, human.parse_line(line))

    def test_no_mail_is_a_decision(self):
        self.assertEqual(self.choose("@driver go"), ("decision", None))

    def test_answers_latest_read_message_from_that_role(self):
        self.deliver("first", type="escalate")
        self.deliver("from nav", sender="navigator", type="question")
        self.deliver("second", type="question")
        self.read_all()
        self.assertEqual(self.choose("@driver go"), ("answer", "0003"))
        self.assertEqual(self.choose("@navigator yes"), ("answer", "0002"))
        self.assertEqual(self.choose("bare text"), ("answer", "0003"))

    def test_other_role_mail_does_not_count(self):
        self.deliver("from nav", sender="navigator", type="question")
        self.read_all()
        self.assertEqual(self.choose("@driver go"), ("decision", None))

    def test_unread_mail_is_not_answered(self):
        self.deliver("old", type="question")
        self.read_all()
        self.deliver("not seen yet", type="question")
        self.assertEqual(self.choose("@driver go"), ("answer", "0001"))

    def test_messages_to_other_inboxes_do_not_count(self):
        self.deliver("to nav", to="navigator")
        maildir.pop(self.sdir, "navigator")
        self.assertEqual(self.choose("@driver go"), ("decision", None))

    def test_numeric_order_past_9999(self):
        cur = maildir.inbox_path(self.sdir, "human", "cur")
        for n in (9999, 10000):
            with open(os.path.join(cur, "%04d.json" % n), "w") as f:
                f.write('{"id": "%04d", "from": "driver", "to": "human", "type": "question", "body": "x"}' % n)
        self.assertEqual(self.choose("@driver go"), ("answer", "10000"))

    def test_unreadable_message_is_skipped(self):
        self.deliver("ok", type="question")
        self.read_all()
        with open(os.path.join(maildir.inbox_path(self.sdir, "human", "cur"), "0002.json"), "w") as f:
            f.write("{broken")
        self.assertEqual(self.choose("@driver go"), ("answer", "0001"))

    def test_explicit_answer_threads(self):
        self.deliver("q", type="question")
        self.read_all()
        self.assertEqual(self.choose("@driver:answer yes"), ("answer", "0001"))

    def test_explicit_answer_without_mail(self):
        self.assertEqual(self.choose("@driver:answer yes"), ("answer", None))

    def test_explicit_other_type_does_not_thread(self):
        self.deliver("q", type="question")
        self.read_all()
        self.assertEqual(self.choose("@driver:question why?"), ("question", None))
        self.assertEqual(self.choose("@driver:decision ship"), ("decision", None))


class TickTest(HumanTestCase):
    def test_shows_new_mail_and_moves_it_to_cur(self):
        self.deliver("Stuck on the BOM fix.\nBoth positions: ...", type="escalate")
        self.deliver("fyi", sender="navigator", type="decision")
        repl = self.repl([])
        self.assertTrue(repl.tick())
        out = self.out.getvalue()
        self.assertIn("#0001 driver → human  escalate  expects reply\n\nStuck on the BOM fix.\nBoth positions: ...", out)
        self.assertIn("(reply: @driver <text>)", out)
        self.assertIn("#0002 navigator → human  decision", out)
        self.assertEqual(out.count("(reply:"), 1)
        self.assertLess(out.index("#0001"), out.index("#0002"))
        self.assertEqual(self.inbox("human"), [])
        self.assertEqual([m["id"] for m in self.inbox("human", "cur")], ["0001", "0002"])

    def test_mail_is_shown_once(self):
        self.deliver("hello", type="question")
        repl = self.repl([], [])
        repl.tick()
        repl.tick()
        self.assertEqual(self.out.getvalue().count("#0001"), 1)

    def test_waits_with_the_tick_timeout(self):
        repl = self.repl([])
        repl.tick()
        repl.tick(timeout=0.25)
        self.assertEqual(self.source.timeouts, [human.TICK, 0.25])
        self.assertEqual(human.TICK, 1.0)

    def test_bare_text_sends_decision_to_driver(self):
        repl = self.repl(["go with option B"])
        self.assertTrue(repl.tick())
        [msg] = self.inbox("driver")
        self.assertEqual((msg["from"], msg["type"], msg["reply_to"], msg["body"]), ("human", "decision", None, "go with option B"))
        self.assertFalse(msg["expects_reply"])
        self.assertIn("sent #0001 to driver (decision)", self.out.getvalue())
        self.assertEqual(self.err.getvalue(), "")

    def test_reply_to_mail_shown_in_the_same_tick(self):
        self.deliver("which option?", sender="navigator", type="question")
        repl = self.repl(["@navigator B"])
        repl.tick()
        [msg] = self.inbox("navigator")
        self.assertEqual((msg["type"], msg["reply_to"]), ("answer", "0001"))
        self.assertIn("sent #0002 to navigator (answer, reply to #0001)", self.out.getvalue())

    def test_explicit_type_send(self):
        repl = self.repl(["@navigator:question is the parser safe?"])
        repl.tick()
        [msg] = self.inbox("navigator")
        self.assertEqual((msg["type"], msg["expects_reply"]), ("question", True))

    def test_message_is_logged(self):
        self.repl(["hi"]).tick()
        records = [r for r in session.read_log(self.sdir) if r["kind"] == "message"]
        self.assertEqual([(r["from"], r["to"], r["body"]) for r in records], [("human", "driver", "hi")])

    def test_several_lines_in_one_batch(self):
        repl = self.repl(["one", "", "@navigator two"])
        self.assertTrue(repl.tick())
        self.assertEqual([m["body"] for m in self.inbox("driver")], ["one"])
        self.assertEqual([m["body"] for m in self.inbox("navigator")], ["two"])

    def test_quit(self):
        repl = self.repl(["/quit", "not sent"])
        self.assertFalse(repl.tick())
        self.assertEqual(self.inbox("driver"), [])

    def test_eof_stops(self):
        repl = self.repl()
        self.assertFalse(repl.tick())

    def test_bad_input_reports_and_continues(self):
        repl = self.repl(["@drvier hi", "/nope", "@driver:ok x", "still sent"])
        self.assertTrue(repl.tick())
        err = self.err.getvalue().splitlines()
        self.assertEqual(len(err), 3)
        self.assertTrue(all(line.startswith("tandem: ") for line in err))
        self.assertIn("unknown role @drvier", err[0])
        self.assertEqual([m["body"] for m in self.inbox("driver")], ["still sent"])

    def test_log_command_reuses_log_rendering(self):
        self.deliver("hello", type="question")
        repl = self.repl(["/log"])
        repl.tick()
        for line in session.log_lines(self.sdir)[0]:
            self.assertIn(maildir.render_record(line), self.out.getvalue())

    def test_status_command_reuses_status_rendering(self):
        self.deliver("hello", type="question")
        repl = self.repl([], ["/status"])
        repl.tick()
        repl.tick()
        self.assertIn(maildir.describe_session(session.load_session(self.dir)), self.out.getvalue())
        self.assertIn("pending: driver 0, navigator 0, human 0", self.out.getvalue())

    def test_help(self):
        self.repl(["/help"]).tick()
        self.assertIn("@role:<type> <text>", self.out.getvalue())

    def test_session_ended_elsewhere_is_reported(self):
        repl = self.repl([], [])
        self.assertTrue(repl.tick())
        self.end()
        self.assertFalse(repl.tick())
        self.assertIn("session %s ended at 2026-11-11T12:00:00Z" % self.dir, self.out.getvalue())
        self.assertEqual(self.source.timeouts, [human.TICK])

    def test_session_replaced_is_reported(self):
        self.end(replaced_by="apix--next")
        self.assertFalse(self.repl([]).tick())
        self.assertIn("replaced by apix--next", self.out.getvalue())

    def test_mail_left_at_the_end_is_still_shown(self):
        self.deliver("final summary", type="done")
        self.end()
        self.assertFalse(self.repl([]).tick())
        out = self.out.getvalue()
        self.assertIn("final summary", out)
        self.assertLess(out.index("final summary"), out.index("ended at"))

    def test_session_ending_during_input_refuses_the_send(self):
        def end_then_type():
            session.end_session(session.load_session(self.dir))
            return ["too late"]

        repl = self.repl(end_then_type)
        self.assertTrue(repl.tick())
        self.assertIn("has ended", self.err.getvalue())
        self.assertEqual(self.inbox("driver"), [])
        self.assertFalse(repl.tick())

    def test_removed_session_is_an_error(self):
        repl = self.repl([])
        os.remove(os.path.join(self.sdir, "session.json"))
        with self.assertRaises(TandemError):
            repl.tick()


class RunTest(HumanTestCase):
    def test_banner_names_session_and_roles(self):
        code = human.run(self.meta, FakeSource(), self.out, self.err)
        self.assertEqual(code, 0)
        self.assertIn("tandem human: session %s (driver claude, navigator agy)" % self.dir, self.out.getvalue())

    def test_runs_until_eof(self):
        src = FakeSource(["a"], [], ["@navigator b"])
        self.assertEqual(human.run(self.meta, src, self.out, self.err), 0)
        self.assertEqual(len(src.timeouts), 4)
        self.assertEqual(len(self.inbox("driver")) + len(self.inbox("navigator")), 2)

    def test_ctrl_c_exits_zero(self):
        src = FakeSource(["a"], then=KeyboardInterrupt())
        self.assertEqual(human.run(self.meta, src, self.out, self.err), 0)
        self.assertEqual(len(self.inbox("driver")), 1)

    def test_custom_tick(self):
        src = FakeSource([])
        human.run(self.meta, src, self.out, self.err, tick=0.01)
        self.assertEqual(src.timeouts, [0.01, 0.01])


class FdLinesTest(TandemTestCase):
    def setUp(self):
        super().setUp()
        self.r, self.w = os.pipe()
        self.addCleanup(self.close)
        self.src = human.FdLines(self.r)

    def close(self):
        for fd in (self.r, self.w):
            try:
                os.close(fd)
            except OSError:
                pass

    def test_timeout_without_input(self):
        self.assertEqual(self.src.poll(0.01), [])

    def test_batch_of_lines_arrives_together(self):
        os.write(self.w, b"one\ntwo\r\nthr")
        self.assertEqual(self.src.poll(1), ["one", "two"])
        os.write(self.w, b"ee\n")
        self.assertEqual(self.src.poll(1), ["three"])

    def test_partial_line_waits(self):
        os.write(self.w, b"part")
        self.assertEqual(self.src.poll(1), [])
        self.assertEqual(self.src.poll(0.01), [])

    def test_eof_flushes_partial_line_then_ends(self):
        os.write(self.w, "café".encode("utf-8"))
        os.close(self.w)
        self.assertEqual(self.src.poll(1), [])
        self.assertEqual(self.src.poll(1), ["café"])
        self.assertIsNone(self.src.poll(1))
        self.assertIsNone(self.src.poll(1))

    def test_eof(self):
        os.close(self.w)
        self.assertIsNone(self.src.poll(1))

    def test_multibyte_split_across_reads(self):
        data = "über\n".encode("utf-8")
        os.write(self.w, data[:1])
        self.assertEqual(self.src.poll(1), [])
        os.write(self.w, data[1:])
        self.assertEqual(self.src.poll(1), ["über"])

    def test_invalid_utf8_is_replaced(self):
        os.write(self.w, b"bad \xff\n")
        self.assertEqual(self.src.poll(1), ["bad �"])

    def test_input_arriving_during_the_wait(self):
        timer = threading.Timer(0.05, os.write, (self.w, b"late\n"))
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertEqual(self.src.poll(2), ["late"])

    def test_stdin_source_picks_fd_or_stream(self):
        self.assertIsInstance(human.stdin_source(io.StringIO("")), human.StreamLines)
        with os.fdopen(os.dup(self.r)) as f:
            self.assertIsInstance(human.stdin_source(f), human.FdLines)

    def test_stream_lines(self):
        src = human.StreamLines(io.StringIO("a\r\nb"))
        self.assertEqual([src.poll(1), src.poll(1), src.poll(1)], [["a"], ["b"], None])


class CliTest(HumanTestCase):
    def test_in_process(self):
        self.deliver("which option?", type="question")
        code, out, err = self.run_cli("human", stdin="B\n/quit\nnot sent\n")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("#0001 driver → human  question", out)
        [msg] = self.inbox("driver")
        self.assertEqual((msg["body"], msg["reply_to"]), ("B", "0001"))

    def test_outside_a_repo_uses_the_only_active_session(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.chdir(outside)
        code, out, err = self.run_cli("human", stdin="@navigator from anywhere\n")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("session %s" % self.dir, out)
        self.assertEqual([m["body"] for m in self.inbox("navigator")], ["from anywhere"])

    def test_outside_a_repo_with_two_sessions_is_an_error(self):
        other = self.make_repo("admin")
        session.init_session(cwd=other, name="other")
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.chdir(outside)
        code, out, err = self.run_cli("human", stdin="hi\n")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("2 active sessions", err)
        self.assertIn("--session", err)

    def test_explicit_session(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.chdir(outside)
        session.init_session(cwd=self.make_repo("admin"), name="other")
        code, out, err = self.run_cli("--session", "fix-csv-bom", "human", stdin="hi\n")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(self.inbox("driver")), 1)

    def test_ended_session_is_refused(self):
        self.end()
        code, out, err = self.run_cli("--session", self.dir, "human", stdin="hi\n")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("has ended", err)

    def test_via_shim_reads_the_real_pipe(self):
        self.deliver("ping", sender="navigator", type="question")
        proc = self.run_shim("human", stdin="pong\n@driver:decision ship it\n", cwd=self.repo)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertIn("#0001 navigator → human  question", proc.stdout)
        self.assertIn("sent #0002 to driver (decision)", proc.stdout)
        self.assertEqual([m["body"] for m in self.inbox("driver")], ["pong", "ship it"])

    def test_help_lists_command(self):
        code, out, err = self.run_cli("--help")
        self.assertIn("human", out)
