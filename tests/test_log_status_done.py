import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from tandem import TandemError, maildir, session
from tests.helpers import SHIM, TandemTestCase


class SessionTestCase(TandemTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("apix")
        self.meta = session.init_session(cwd=self.repo, name="fix-csv-bom", ticket="APIX-123", task="Strip the BOM.")
        self.dir = self.meta["dir"]
        self.sdir = session.session_path(self.dir)
        os.chdir(self.repo)

    def deliver(self, body="hello", sender="driver", to="navigator", meta=None, **kw):
        return maildir.deliver(meta or self.meta, maildir.build_envelope(sender, to, body, **kw))

    def console(self, body, to="driver", peer="claude"):
        record = {"kind": "console", "ts": "2026-11-11T10:05:00Z", "from": "human", "to": to, "peer": peer, "body": body}
        with session.locked(self.sdir):
            session.write_log_line(self.sdir, record)

    def append_raw(self, text, sdir=None):
        with open(os.path.join(sdir or self.sdir, "log.jsonl"), "a", encoding="utf-8") as f:
            f.write(text)

    def ok(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        return out

    def error(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 1, out)
        self.assertEqual(out, "")
        return err

    def write_pidfile(self, role, text, sdir=None):
        path = session.watcher_pidfile(sdir or self.sdir, role)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)


class LogRenderTest(SessionTestCase):
    def test_init_event(self):
        out = self.ok("log")
        self.assertEqual(len(out.splitlines()), 1)
        self.assertRegex(out, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ  \* init session=apix--fix-csv-bom repo=")
        self.assertIn("repo=%s" % self.repo, out)

    def test_message_header_refs_and_indented_body(self):
        self.deliver("Plan v1\n\n- step one", type="plan", files=["plan.md"])
        self.deliver("ok", sender="navigator", to="driver", reply_to="1")
        lines = self.ok("log").splitlines()[1:]
        ts = session.read_log(self.sdir)[1]["ts"]
        self.assertEqual(lines[0], "%s  #0001 driver → navigator  plan  expects reply" % ts)
        self.assertEqual(lines[1:5], ["    files: plan.md", "    Plan v1", "", "    - step one"])
        self.assertTrue(lines[5].endswith("#0002 navigator → driver  answer  reply to #0001"))
        self.assertEqual(lines[6], "    ok")
        self.assertEqual(len(lines), 7)

    def test_console_record(self):
        self.console("use the stdlib csv\nnot pandas")
        lines = self.ok("log").splitlines()[1:]
        self.assertEqual(
            lines,
            [
                "2026-11-11T10:05:00Z  human → driver  console (claude)",
                "    use the stdlib csv",
                "    not pandas",
            ],
        )

    def test_event_fields(self):
        session.log_event(self.sdir, "watcher_started", role="navigator", pid=42, extra={"a": 1})
        last = self.ok("log").splitlines()[-1]
        self.assertTrue(last.endswith('  * watcher_started role=navigator pid=42 extra={"a": 1}'), last)

    def test_unknown_and_broken_lines_shown_raw(self):
        self.append_raw('not json\n{"kind":"weird"}\n[1,2]\n')
        lines = self.ok("log").splitlines()[1:]
        self.assertEqual(lines, ["? not json", '? {"kind":"weird"}', "? [1,2]"])

    def test_json_passthrough(self):
        self.deliver("hi")
        self.append_raw("not json\n")
        with open(os.path.join(self.sdir, "log.jsonl"), encoding="utf-8") as f:
            stored = f.read()
        self.assertEqual(self.ok("log", "--json"), stored)

    def test_partial_last_line_not_printed(self):
        self.append_raw('{"kind":"event","ts":"x","event":"half')
        self.assertEqual(len(self.ok("log").splitlines()), 1)
        self.assertEqual(len(self.ok("log", "--json").splitlines()), 1)

    def test_line_separator_in_body_does_not_split(self):
        self.deliver("one\u2028two")
        out = self.ok("log", "--json")
        self.assertEqual(len(out.split("\n")), 3)
        self.assertEqual(json.loads(out.split("\n")[1])["body"], "one\u2028two")

    def test_ended_session_via_session_flag(self):
        self.deliver("hi")
        session.end_session(self.meta)
        self.assertIn("* done session=%s" % self.dir, self.ok("--session", "fix-csv-bom", "log"))

    def test_no_session(self):
        os.chdir(self.tmp)
        self.mark_ended()
        self.assertIn("no active session", self.error("log"))

    def test_shim(self):
        result = self.run_shim("log", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("* init", result.stdout)


class LogFollowTest(SessionTestCase):
    def follow_in_thread(self, meta=None):
        seen = []
        thread = threading.Thread(
            target=maildir.follow_log,
            args=(meta or self.meta, seen.append),
            kwargs={"follow": True, "interval": 0.02},
            daemon=True,
        )
        thread.start()
        return thread, seen

    def test_follow_sees_appends_and_stops_at_done(self):
        thread, seen = self.follow_in_thread()
        self.wait_for(lambda: len(seen) == 1)
        self.deliver("first")
        self.wait_for(lambda: len(seen) == 2)
        self.deliver("second", sender="navigator", to="driver")
        self.wait_for(lambda: len(seen) == 3)
        session.end_session(self.meta)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        kinds = [json.loads(line).get("event") or json.loads(line)["body"] for line in seen]
        self.assertEqual(kinds, ["init", "first", "second", "done"])

    def test_partial_line_waits_for_newline(self):
        thread, seen = self.follow_in_thread()
        self.wait_for(lambda: len(seen) == 1)
        self.append_raw('{"kind":"event","ts":"t","event":"sp')
        time.sleep(0.1)
        self.assertEqual(len(seen), 1)
        self.append_raw('lit"}\n')
        self.wait_for(lambda: len(seen) == 2)
        self.assertEqual(json.loads(seen[1])["event"], "split")
        self.mark_ended()
        thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_unreadable_session_json_drains_and_returns(self):
        self.deliver("hi")
        with open(os.path.join(self.sdir, "session.json"), "w") as f:
            f.write("{")
        seen = []
        maildir.follow_log(self.meta, seen.append, follow=True, interval=0.02)
        self.assertEqual(len(seen), 2)

    def test_ended_session_drains_and_returns(self):
        self.deliver("hi")
        session.end_session(self.meta)
        seen = []
        maildir.follow_log(self.meta, seen.append, follow=True, interval=0.02)
        self.assertEqual(len(seen), 3)

    def test_cli_follow_via_shim(self):
        proc = subprocess.Popen(
            [sys.executable, SHIM, "log", "-f"],
            cwd=self.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            env=dict(os.environ),
        )
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True)
        reader.start()

        def next_line():
            # A bounded wait, so a follow that stops printing fails instead of hanging the suite.
            try:
                return lines.get(timeout=10)
            except queue.Empty:
                self.fail("log -f printed nothing within 10s")

        self.assertIn("* init", next_line())
        self.deliver("live message")
        self.assertIn("#0001 driver → navigator", next_line())
        self.assertEqual(next_line(), "    live message\n")
        session.end_session(self.meta)
        self.assertEqual(proc.wait(timeout=10), 0, proc.stderr.read())
        reader.join(5)
        rest = []
        while not lines.empty():
            rest.append(lines.get())
        self.assertIn("* done session=%s" % self.dir, "".join(rest))
        proc.stdout.close()
        proc.stderr.close()


class PidfileTest(SessionTestCase):
    def read(self, text):
        self.write_pidfile("driver", text)
        return session.read_watcher(self.sdir, "driver")

    def test_shape(self):
        self.assertEqual(self.read('{"pid": 77, "ls_address": "127.0.0.1:1"}'), {"pid": 77, "ls_address": "127.0.0.1:1"})
        self.assertEqual(self.read('{"pid": 78, "ls_address": null}'), {"pid": 78, "ls_address": None})

    def test_bad_contents(self):
        for text in ("", "abc", "123", "0", "true", '{"ls_address": "x"}', '{"pid": "12"}', '{"pid": 0}', '{"pid": true}', "[1]"):
            self.assertIsNone(self.read(text), text)

    def test_missing(self):
        self.assertIsNone(session.read_watcher(self.sdir, "driver"))

    def test_liveness(self):
        self.assertTrue(session.pid_alive(os.getpid()))
        self.assertFalse(session.pid_alive(self.dead_pid()))
        # pid 1 belongs to root: signalling it is refused, which still means it is alive.
        self.assertTrue(session.pid_alive(1))


class StatusTest(SessionTestCase):
    def test_zero_sessions(self):
        session.end_session(self.meta)
        self.assertEqual(self.ok("status"), "no active sessions\n")

    def test_zero_sessions_empty_home(self):
        shutil.rmtree(self.tandem_home)
        self.assertEqual(self.ok("status"), "no active sessions\n")

    def test_one_session(self):
        self.deliver("plan", type="plan")
        self.deliver("q1", sender="navigator", to="driver")
        self.deliver("q2", sender="navigator", to="human", type="escalate")
        maildir.pop(self.sdir, "navigator")
        out = self.ok("status")
        log = {r.get("id"): r for r in session.read_log(self.sdir)}
        self.assertEqual(
            out.splitlines(),
            [
                "%s  %s" % (self.dir, self.repo),
                "  name fix-csv-bom, ticket APIX-123, created %s" % self.meta["created"],
                "  roles: driver claude, navigator agy",
                "  pending: driver 1, navigator 0, human 1",
                "  last from driver: #0001 plan → navigator  %s" % log["0001"]["ts"],
                "  last from navigator: #0003 escalate → human  %s" % log["0003"]["ts"],
                "  last from human: none",
                "  watchers: driver not running, navigator not running",
            ],
        )

    def test_watchers(self):
        self.write_pidfile("driver", json.dumps({"pid": os.getpid(), "ls_address": None}))
        self.write_pidfile("navigator", json.dumps({"pid": self.dead_pid(), "ls_address": "127.0.0.1:9"}))
        line = self.ok("status").splitlines()[-1]
        self.assertRegex(line, r"^  watchers: driver running \(pid %d\), navigator dead \(stale pid \d+\)$" % os.getpid())
        self.write_pidfile("navigator", "garbage")
        self.assertTrue(self.ok("status").splitlines()[-1].endswith("navigator unreadable pidfile"))

    def test_several_sessions_ended_excluded(self):
        other_repo = self.make_repo("admin", branch="feat/retry")
        other = session.init_session(cwd=other_repo)
        third_repo = self.make_repo("gone", branch="feat/old")
        third = session.init_session(cwd=third_repo)
        session.end_session(third)
        out = self.ok("status")
        heads = [line for line in out.splitlines() if line and not line.startswith(" ")]
        self.assertEqual(heads, ["%s  %s" % (other["dir"], other_repo), "%s  %s" % (self.dir, self.repo)])
        self.assertNotIn(third["dir"], out)
        self.assertNotIn("stale", out)

    def test_stale_pointers(self):
        ended_repo = self.make_repo("ended", branch="feat/a")
        ended = session.init_session(cwd=ended_repo)
        self.mark_ended(ended["dir"])
        missing_repo = self.make_repo("missing", branch="feat/b")
        missing = session.init_session(cwd=missing_repo)
        shutil.rmtree(session.session_path(missing["dir"]))
        out = self.ok("status")
        self.assertIn(self.dir + "  ", out)
        stale = out.split("\n\n")[-1].splitlines()
        self.assertTrue(stale[0].startswith("stale repo pointers"))
        self.assertEqual(
            sorted(stale[1:]),
            sorted(
                [
                    "  %s → %s (ended)" % (ended_repo, ended["dir"]),
                    "  %s → %s (missing)" % (missing_repo, missing["dir"]),
                ]
            ),
        )

    def test_stale_pointer_to_an_unreadable_session(self):
        with open(os.path.join(self.sdir, "session.json"), "w") as f:
            f.write("{")
        out = self.ok("status")
        self.assertTrue(out.startswith("no active sessions\n\nstale repo pointers"), out)
        self.assertIn("%s → %s (unreadable)" % (self.repo, self.dir), out)

    def test_stale_pointer_with_no_sessions(self):
        self.mark_ended()
        out = self.ok("status")
        self.assertTrue(out.startswith("no active sessions\n\nstale repo pointers"))
        self.assertIn("%s → %s (ended)" % (self.repo, self.dir), out)

    def test_session_flag_narrows_and_shows_ended(self):
        other = session.init_session(cwd=self.make_repo("admin", branch="feat/retry"))
        session.end_session(self.meta)
        out = self.ok("--session", self.dir, "status")
        self.assertTrue(out.startswith("%s  %s  (ended " % (self.dir, self.repo)))
        self.assertNotIn(other["dir"], out)

    def test_outside_repo(self):
        os.chdir(self.tmp)
        self.assertIn(self.dir, self.ok("status"))

    def test_no_ticket_omitted(self):
        session.end_session(self.meta)
        meta = session.init_session(cwd=self.repo, name="plain")
        self.assertIn("  name plain, created %s" % meta["created"], self.ok("status"))


class DoneTest(SessionTestCase):
    def summary(self):
        with open(os.path.join(self.sdir, "summary.md"), encoding="utf-8") as f:
            return f.read()

    def test_ends_session_logs_and_clears_pointer(self):
        out = self.ok("done")
        self.assertEqual(out, os.path.join(self.sdir, "summary.md") + "\n")
        meta = session.load_session(self.dir)
        self.assertIsNotNone(meta["ended"])
        last = session.read_log(self.sdir)[-1]
        self.assertEqual(last, {"kind": "event", "ts": meta["ended"], "event": "done", "session": self.dir})
        self.assertIsNone(session.read_pointer(self.repo))
        self.assertFalse(os.path.exists(session.repo_pointer_dir(self.repo)))

    def test_summary_contents(self):
        self.deliver("Plan v1\nmore detail", type="plan")
        self.deliver("Risk: none.\nApproved.", sender="navigator", to="driver", reply_to="1", type="review")
        self.console("go with option B\n\nthanks")
        self.deliver("Done: BOM stripped.\nTests green.", to="human", type="done")
        with open(os.path.join(self.sdir, "decisions.md"), "w", encoding="utf-8") as f:
            f.write("# Decisions\n\n- csv module, not pandas\n")
        self.ok("done")
        text = self.summary()
        meta = session.load_session(self.dir)
        log = {r.get("id"): r for r in session.read_log(self.sdir)}
        self.assertTrue(text.startswith("# fix-csv-bom\n\n- session: `%s`\n" % self.dir))
        for line in (
            "- repo: `%s`" % self.repo,
            "- ticket: APIX-123",
            "- roles: driver claude, navigator agy",
            "- started: %s" % meta["created"],
            "- ended: %s" % meta["ended"],
            "## Task\n\nStrip the BOM.\n",
            "## Messages (3)\n",
            "- `#0001` %s driver → navigator, plan: Plan v1\n" % log["0001"]["ts"],
            "- `#0002` %s navigator → driver, review, reply to #0001: Risk: none.\n" % log["0002"]["ts"],
            "- `#0003` %s driver → human, done: Done: BOM stripped.\n" % log["0003"]["ts"],
            "## Human input in consoles (1)\n\n- 2026-11-11T10:05:00Z, to driver (claude console):\n\n"
            "  > go with option B\n  >\n  > thanks\n",
            "## Wrap-up\n\nFrom driver (`#0003`):\n\n> Done: BOM stripped.\n> Tests green.\n",
            "## Decisions\n\n# Decisions\n\n- csv module, not pandas\n",
        ):
            self.assertIn(line, text)
        self.assertTrue(text.endswith("- csv module, not pandas\n"))
        self.assertNotIn("\n\n\n", text)

    def test_summary_without_task_ticket_or_messages(self):
        session.end_session(self.meta)
        meta = session.init_session(cwd=self.repo, name="bare")
        session.end_session(meta)
        with open(os.path.join(session.session_path(meta["dir"]), "summary.md"), encoding="utf-8") as f:
            text = f.read()
        for line in (
            "- ticket: none",
            "## Task\n\n_No task brief was recorded._",
            "## Messages (0)\n\n_No messages._",
            "## Human input in consoles (0)\n\n_None._",
            "## Decisions\n\n_decisions.md was not written._\n",
        ):
            self.assertIn(line, text)
        self.assertNotIn("Wrap-up", text)

    def test_summary_survives_a_broken_log_line_and_unreadable_decisions(self):
        self.deliver("Plan v1", type="plan")
        self.append_raw("not json\n[1, 2]\n")
        self.deliver("Approved.", sender="navigator", to="driver", reply_to="1", type="review")
        with open(os.path.join(self.sdir, "decisions.md"), "wb") as f:
            f.write(b"\xff\xfe not utf-8\n")
        self.ok("done")
        text = self.summary()
        self.assertIn("## Messages (2)\n", text)
        self.assertIn("## Decisions\n\n_decisions.md could not be read: ", text)

    def test_missing_log_reads_as_empty(self):
        os.unlink(os.path.join(self.sdir, "log.jsonl"))
        self.assertEqual(session.read_log(self.sdir), [])
        self.assertEqual(session.log_lines(self.sdir, 10), ([], 10))

    def test_long_subject_capped(self):
        self.deliver("\n   \n" + "x" * 300)
        session.end_session(self.meta)
        line = [line for line in self.summary().splitlines() if line.startswith("- `#0001`")][0]
        subject = line.split(", question: ", 1)[1]
        self.assertEqual(len(subject), session.SUBJECT_CAP)
        self.assertTrue(subject.endswith("…"))

    def test_already_ended(self):
        self.ok("done")
        before = self.summary()
        err = self.error("--session", self.dir, "done")
        self.assertIn("already ended", err)
        self.assertEqual(self.summary(), before)
        self.assertEqual(sum(1 for r in session.read_log(self.sdir) if r.get("event") == "done"), 1)

    def test_already_ended_via_pointer_gone(self):
        self.ok("done")
        self.assertIn("no active session", self.error("done"))

    def test_send_refused_after_done(self):
        self.ok("done")
        code, _, err = self.run_cli("--session", self.dir, "send", "--as", "driver", "--to", "navigator", "late")
        self.assertEqual(code, 1)
        self.assertIn("has ended", err)

    def test_init_after_done_needs_no_replace(self):
        self.ok("done")
        meta = session.init_session(cwd=self.repo, name="next")
        self.assertEqual(session.read_pointer(self.repo), meta["dir"])

    def test_pointer_moved_on_is_kept(self):
        session.write_pointer(self.repo, "apix--other")
        session.end_session(self.meta)
        self.assertEqual(session.read_pointer(self.repo), "apix--other")

    def test_from_outside_repo_via_env(self):
        os.chdir(self.tmp)
        os.environ["TANDEM_SESSION"] = self.dir
        self.ok("done")
        self.assertIsNone(session.read_pointer(self.repo))

    def test_no_session(self):
        os.chdir(self.tmp)
        session.end_session(self.meta)
        self.assertIn("no active session", self.error("done"))

    def test_session_removed_meanwhile(self):
        meta = dict(self.meta)
        os.remove(os.path.join(self.sdir, "session.json"))
        with self.assertRaises(TandemError) as cm:
            session.end_session(meta)
        self.assertIn("no longer exists", str(cm.exception))

    def test_sends_racing_done_all_reach_summary(self):
        senders = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import subprocess, sys\n"
                    "for i in range(10):\n"
                    "    r = subprocess.run([sys.executable, sys.argv[1], '--session', sys.argv[2], 'send',"
                    " '--as', 'driver', '--to', 'navigator', 'race %s' % i], stdout=subprocess.DEVNULL,"
                    " stderr=subprocess.PIPE, universal_newlines=True)\n"
                    "    print(r.returncode, r.stderr.strip(), flush=True)\n",
                    SHIM,
                    self.dir,
                ],
                stdout=subprocess.PIPE,
                universal_newlines=True,
                env=dict(os.environ),
            )
            for _ in range(3)
        ]
        # End mid-stream: after the first send landed, while the rest are still coming.
        deadline = time.monotonic() + 30
        while not maildir._message_ids(self.sdir):
            self.assertLess(time.monotonic(), deadline, "no send landed")
            time.sleep(0.01)
        session.end_session(self.meta)
        results = []
        for proc in senders:
            out, _ = proc.communicate(timeout=60)
            results += out.splitlines()
        delivered = sorted(maildir._message_ids(self.sdir))
        self.assertTrue(0 < len(delivered) < 30, delivered)
        self.assertEqual(len([r for r in results if r.startswith("0 ")]), len(delivered))
        self.assertTrue(any("has ended" in r for r in results), results)
        text = self.summary()
        self.assertIn("## Messages (%d)" % len(delivered), text)
        for n in delivered:
            self.assertIn("`#%s`" % maildir.format_id(n), text)
        self.assertEqual(session.read_log(self.sdir)[-1]["event"], "done")

    def test_shim(self):
        result = self.run_shim("done", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.exists(result.stdout.strip()))


class CommandsRegisteredTest(SessionTestCase):
    def test_help_lists_commands(self):
        result = self.run_shim("--help")
        for name in ("log", "status", "done"):
            self.assertIn(name, result.stdout)

    def test_log_rejects_unknown_option(self):
        code, _, err = self.run_cli("log", "--bogus")
        self.assertEqual(code, 1)
        self.assertIn("--bogus", err)
