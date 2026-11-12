import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from unittest import mock

from tandem import maildir, session, watch
from tests.helpers import TandemTestCase

FAST = 0.01
DOORBELL = "tandem doorbell: new mail for navigator. Run `tandem recv`."

FAKE_AGENTAPI = """#!%(python)s
import json, os, sys
state = %(state)r
with open(os.path.join(state, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "ls": os.environ.get("ANTIGRAVITY_LS_ADDRESS")}) + "\\n")
try:
    with open(os.path.join(state, "mode")) as f:
        mode = f.read().strip()
except OSError:
    mode = "ok"
if mode == "fail":
    print(json.dumps({"error": "conversation not found"}))
    sys.exit(1)
if mode == "error-json":
    print(json.dumps({"error": "conversation not found"}))
    sys.exit(0)
print(json.dumps({"sendMessage": {}}))
"""


class FakeLS:
    """A loopback listener standing in for agy's language server."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        # Closing a socket another thread is blocked in accept() on stalls on
        # macOS, so the accept loop polls a stop flag instead.
        self.sock.settimeout(0.02)
        self.port = self.sock.getsockname()[1]
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.stopping.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.close()

    def close(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.thread.join(5)
        self.sock.close()


class AgyWatchTestCase(TandemTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("apix")
        # A unique name puts a unique --session in every watcher's command
        # line, so teardown can find this test's detached processes by it.
        self.meta = session.init_session(cwd=self.repo, name="agy-" + uuid.uuid4().hex[:10])
        self.dir = self.meta["dir"]
        self.sdir = session.session_path(self.dir)
        os.chdir(self.repo)

        self.ls = FakeLS()
        self.addCleanup(self.ls.close)
        self.address = "127.0.0.1:%d" % self.ls.port

        self.state = os.path.join(self.tmp, "agentapi")
        os.makedirs(self.state)
        self.exe = os.path.join(self.state, "agy")
        with open(self.exe, "w") as f:
            f.write(FAKE_AGENTAPI % {"python": sys.executable, "state": self.state})
        os.chmod(self.exe, 0o755)

        os.environ["ANTIGRAVITY_LS_ADDRESS"] = self.address
        os.environ["ANTIGRAVITY_CSRF_TOKEN"] = "test-token"
        os.environ["ANTIGRAVITY_AGENTAPI_EXE"] = self.exe
        self.addCleanup(self.reap_watchers)

    # helpers

    def deliver(self, to="navigator", sender="driver", body="hello"):
        return maildir.deliver(self.meta, maildir.build_envelope(sender, to, body))

    def set_conversation(self, conv):
        os.makedirs(os.path.join(self.sdir, "peers", "agy"), exist_ok=True)
        session.write_text_atomic(watch.conversation_path(self.sdir), conv + "\n")

    def set_mode(self, mode):
        with open(os.path.join(self.state, "mode"), "w") as f:
            f.write(mode)

    def calls(self):
        try:
            with open(os.path.join(self.state, "calls.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def pidfile(self, role="navigator"):
        return session.watcher_pidfile(self.sdir, role)

    def write_pidfile(self, record, role="navigator"):
        os.makedirs(os.path.dirname(self.pidfile(role)), exist_ok=True)
        text = record if isinstance(record, str) else json.dumps(record)
        session.write_text_atomic(self.pidfile(role), text + "\n")

    def live_process(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

        def stop():
            proc.kill()
            proc.wait()

        self.addCleanup(stop)
        return proc.pid

    def serve_in_thread(self, **kw):
        """Run `watch.serve` in a thread; returns (thread, result dict, log lines)."""
        result, lines = {}, []
        kw.setdefault("interval", FAST)
        kw.setdefault("ls_interval", FAST)
        kw.setdefault("retry_interval", FAST)
        kw.setdefault("log", lines.append)

        def run():
            result["reason"] = watch.serve(self.meta, "navigator", kw.pop("address", self.address), **kw)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def stop():
            if thread.is_alive():
                self.mark_ended()
                thread.join(5)

        self.addCleanup(stop)
        return thread, result, lines

    def finish(self, thread):
        self.mark_ended()
        thread.join(5)
        self.assertFalse(thread.is_alive())

    # detached processes

    def watcher_processes(self):
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="], stdout=subprocess.PIPE, universal_newlines=True, check=True
        ).stdout
        found = []
        for line in out.splitlines():
            pid, _, command = line.strip().partition(" ")
            if self.dir in command and " watch " in command and int(pid) != os.getpid():
                found.append(int(pid))
        return found

    def reap_watchers(self):
        pids = self.watcher_processes()
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        end = time.monotonic() + 5
        while self.watcher_processes() and time.monotonic() < end:
            time.sleep(0.05)
        leftover = self.watcher_processes()
        for pid in leftover:
            os.kill(pid, signal.SIGKILL)
        if leftover:
            time.sleep(0.1)
        self.assertEqual(self.watcher_processes(), [], "stray tandem watch processes")

    def detach(self, *extra):
        start = time.monotonic()
        proc = self.run_shim(
            "--session", self.dir, "watch", "--as", "navigator", "--detach",
            "--poll-interval", "0.02", "--ls-check-interval", "0.05", *extra
        )
        return proc, time.monotonic() - start

    def started_pid(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        match = re.search(r"started for navigator \(pid (\d+)\)", proc.stdout)
        self.assertIsNotNone(match, proc.stdout)
        return int(match.group(1))


class EnvTest(AgyWatchTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(watch, "detach", side_effect=AssertionError("must not fork in-process"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def error(self, *argv):
        code, out, err = self.run_cli("--session", self.dir, "watch", "--as", "navigator", "--detach", *argv)
        self.assertEqual(code, 1, err)
        self.assertEqual(out, "")
        return err

    def test_missing_ls_address(self):
        del os.environ["ANTIGRAVITY_LS_ADDRESS"]
        err = self.error()
        self.assertIn("ANTIGRAVITY_LS_ADDRESS not set", err)
        self.assertIn("started by agy itself", err)
        self.assertFalse(os.path.exists(self.pidfile()))

    def test_missing_csrf_token(self):
        os.environ["ANTIGRAVITY_CSRF_TOKEN"] = ""
        self.assertIn("ANTIGRAVITY_CSRF_TOKEN not set", self.error())

    def test_both_missing(self):
        del os.environ["ANTIGRAVITY_LS_ADDRESS"]
        del os.environ["ANTIGRAVITY_CSRF_TOKEN"]
        self.assertIn("ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CSRF_TOKEN not set", self.error())

    def test_unparseable_ls_address(self):
        for value in ("localhost", "localhost:", "localhost:http", ":50444", "localhost:70000", "http://"):
            self.assertIsNone(watch.parse_ls_address(value), value)
            # Unknown form, not a dead server: the watcher keeps serving.
            self.assertTrue(watch.ls_alive(value), value)

    def test_ls_address_forms(self):
        self.assertEqual(watch.parse_ls_address("localhost:50444"), ("localhost", 50444))
        self.assertEqual(watch.parse_ls_address("[::1]:50444"), ("::1", 50444))
        self.assertEqual(watch.parse_ls_address("http://127.0.0.1:50444"), ("127.0.0.1", 50444))
        self.assertEqual(watch.parse_ls_address("https://127.0.0.1:50444/ls/"), ("127.0.0.1", 50444))
        self.assertEqual(watch.parse_ls_address(" [::1]:1 "), ("::1", 1))

    def test_url_form_ls_address_is_checked(self):
        port = self.ls.port
        self.assertTrue(watch.ls_alive("http://127.0.0.1:%d" % port))
        self.ls.close()
        self.assertFalse(watch.ls_alive("http://127.0.0.1:%d" % port))

    def test_bad_intervals(self):
        self.assertIn("--poll-interval", self.error("--poll-interval", "0"))
        self.assertIn("--ls-check-interval", self.error("--ls-check-interval", "soon"))

    def test_intervals_need_detach(self):
        code, _, err = self.run_cli("--session", self.dir, "watch", "--as", "navigator", "--once", "--poll-interval", "1")
        self.assertEqual(code, 1)
        self.assertIn("need --detach", err)

    def test_deadline_needs_once(self):
        self.assertIn("--deadline needs --once", self.error("--deadline", "5"))

    def test_once_and_detach_are_exclusive(self):
        self.assertIn("not allowed", self.error("--once"))

    def test_human_has_no_watcher(self):
        code, _, err = self.run_cli("--session", self.dir, "watch", "--as", "human", "--detach")
        self.assertEqual(code, 1)
        self.assertIn("driver, navigator", err)

    def test_ended_session_starts_nothing(self):
        self.mark_ended()
        code, out, err = self.run_cli("--session", self.dir, "watch", "--as", "navigator", "--detach")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("has ended", out)
        self.assertFalse(os.path.exists(self.pidfile()))

    def test_agentapi_exe_defaults_to_agy(self):
        self.assertEqual(watch.agentapi_exe({}), "agy")
        self.assertEqual(watch.agentapi_exe({"ANTIGRAVITY_AGENTAPI_EXE": "/x/agy"}), "/x/agy")


class RingTest(AgyWatchTestCase):
    def test_ring_reaches_the_current_conversation(self):
        self.set_conversation("conv-1")
        self.deliver(body="SECRET body text")
        thread, result, lines = self.serve_in_thread()
        self.wait_for(lambda: watch.read_rung(self.sdir, "navigator") == 1)
        self.assertEqual(
            self.calls(), [{"argv": ["agentapi", "send-message", "conv-1", DOORBELL], "ls": self.address}]
        )
        self.finish(thread)
        self.assertEqual(result["reason"], "session ended")
        self.assertNotIn("SECRET", "\n".join(lines))
        self.assertIn("rang conv-1 for #0001", lines)

    def test_ring_follows_a_changed_conversation_id(self):
        self.set_conversation("conv-1")
        self.deliver()
        thread, _, _ = self.serve_in_thread()
        self.wait_for(lambda: len(self.calls()) == 1)
        self.set_conversation("conv-2")
        self.deliver()
        self.wait_for(lambda: watch.read_rung(self.sdir, "navigator") == 2)
        self.assertEqual([c["argv"][2] for c in self.calls()], ["conv-1", "conv-2"])
        self.finish(thread)

    def test_rung_message_is_not_rung_again(self):
        self.set_conversation("conv-1")
        self.deliver()
        thread, _, _ = self.serve_in_thread()
        self.wait_for(lambda: len(self.calls()) == 1)
        time.sleep(0.1)
        self.assertEqual(len(self.calls()), 1)
        self.finish(thread)

    def test_env_conversation_id_is_the_fallback(self):
        os.environ["ANTIGRAVITY_CONVERSATION_ID"] = "conv-env"
        self.deliver()
        thread, _, _ = self.serve_in_thread()
        self.wait_for(lambda: len(self.calls()) == 1)
        self.assertEqual(self.calls()[0]["argv"][2], "conv-env")
        self.finish(thread)

    def test_no_conversation_id_waits_without_ringing(self):
        self.deliver()
        thread, _, lines = self.serve_in_thread()
        self.wait_for(lambda: lines and "no conversation id" in lines[-1])
        self.assertEqual(self.calls(), [])
        self.assertEqual(watch.read_rung(self.sdir, "navigator"), 0)
        self.set_conversation("conv-late")
        self.wait_for(lambda: watch.read_rung(self.sdir, "navigator") == 1)
        self.finish(thread)

    def test_other_inboxes_do_not_ring(self):
        self.set_conversation("conv-1")
        self.deliver(to="driver", sender="navigator")
        self.deliver(to="human")
        thread, _, _ = self.serve_in_thread()
        time.sleep(0.1)
        self.assertEqual(self.calls(), [])
        self.finish(thread)

    def test_status_shows_the_watcher_running(self):
        thread, _, _ = self.serve_in_thread()
        self.wait_for(lambda: os.path.exists(self.pidfile()))
        self.assertEqual(maildir.watcher_state(self.sdir, "navigator"), "running (pid %d)" % os.getpid())
        self.finish(thread)
        self.assertEqual(maildir.watcher_state(self.sdir, "navigator"), "not running")


CONV_A = "aaaaaaaa-1111-2222-3333-444444444444"
CONV_B = "bbbbbbbb-1111-2222-3333-444444444444"


class PresenceTest(AgyWatchTestCase):
    """The watcher rings the conversation agy shows, read from the presence lock agy holds."""

    def hold_lock(self, conv):
        presence = os.path.join(self.tmp, "presence")
        os.makedirs(presence, exist_ok=True)
        f = open(os.path.join(presence, conv + ".lock"), "w")
        self.addCleanup(f.close)
        return f

    def test_ring_follows_the_held_lock_over_the_hooks_file(self):
        # As after `/new`: the hooks' file still names the old conversation.
        self.set_conversation("conv-old")
        a = self.hold_lock(CONV_A)
        self.deliver()
        thread, _, lines = self.serve_in_thread(agy_pid=os.getpid())
        self.wait_for(lambda: watch.read_rung(self.sdir, "navigator") == 1)
        a.close()
        self.hold_lock(CONV_B)
        self.deliver()
        self.wait_for(lambda: watch.read_rung(self.sdir, "navigator") == 2)
        self.assertEqual([c["argv"][2] for c in self.calls()], [CONV_A, CONV_B])
        self.assertIn("agy is pid %d; ringing the conversation it shows" % os.getpid(), lines)
        self.finish(thread)

    def test_no_single_lock_falls_back_to_the_hooks_file(self):
        self.set_conversation("conv-1")
        self.hold_lock(CONV_A)
        self.hold_lock(CONV_B)
        self.deliver()
        thread, _, _ = self.serve_in_thread(agy_pid=os.getpid())
        self.wait_for(lambda: len(self.calls()) == 1)
        self.assertEqual(self.calls()[0]["argv"][2], "conv-1")
        self.finish(thread)

    def test_without_agy_pid_the_hooks_file_is_used(self):
        self.set_conversation("conv-1")
        self.hold_lock(CONV_A)
        self.deliver()
        thread, _, lines = self.serve_in_thread()
        self.wait_for(lambda: len(self.calls()) == 1)
        self.assertEqual(self.calls()[0]["argv"][2], "conv-1")
        self.assertTrue(any("no agy process found" in line for line in lines))
        self.finish(thread)

    def test_open_paths_sees_a_held_lock(self):
        self.hold_lock(CONV_A)
        self.assertEqual(watch.presence_conversation(os.getpid()), CONV_A)

    def test_presence_conversation_needs_exactly_one_lock(self):
        lock = "/h/.gemini/antigravity-cli/presence/%s.lock"
        cases = [
            ([lock % CONV_A, "/h/x.db", lock % CONV_A], CONV_A),
            ([lock % CONV_A, lock % CONV_B], None),
            (["/h/conversations/%s.db" % CONV_A], None),
            ([lock % "not-a-uuid", "/h/presence/%s.lock.tmp" % CONV_A], None),
            ([], None),
        ]
        for paths, want in cases:
            self.assertEqual(watch.presence_conversation(1, paths=lambda pid: paths), want, paths)

    def test_find_agy_pid_walks_up_to_agy(self):
        tree = {300: (200, "python3.15"), 200: (100, "sh"), 100: (50, "agy"), 50: (1, "zsh")}
        self.assertEqual(watch.find_agy_pid(300, tree.get), 100)
        self.assertIsNone(watch.find_agy_pid(200, {200: (1, "sh")}.get))
        self.assertIsNone(watch.find_agy_pid(300, {}.get))
        deep = {n: (n - 1, "sh") for n in range(2, 40)}
        self.assertIsNone(watch.find_agy_pid(39, deep.get))

    def test_watcher_agy_pid_override(self):
        never = lambda pid: self.fail("searched despite TANDEM_AGY_PID")
        self.assertIsNone(watch.watcher_agy_pid({"TANDEM_AGY_PID": ""}, never))
        self.assertEqual(watch.watcher_agy_pid({"TANDEM_AGY_PID": "4242"}, never), 4242)
        self.assertIsNone(watch.watcher_agy_pid({"TANDEM_AGY_PID": "x"}, never))
        self.assertIsNone(watch.watcher_agy_pid({}, lambda pid: (1, "sh")))

    def test_parent_and_name_reads_this_process(self):
        self.assertEqual(watch.parent_and_name(os.getpid())[0], os.getppid())


class FailureTest(AgyWatchTestCase):
    def test_failed_send_is_not_marked_rung_and_is_retried(self):
        self.set_conversation("conv-1")
        self.set_mode("fail")
        self.deliver()
        thread, _, lines = self.serve_in_thread()
        self.wait_for(lambda: len(self.calls()) >= 3)
        self.assertEqual(watch.read_rung(self.sdir, "navigator"), 0)
        self.assertTrue(any("failed: exit 1: conversation not found" in line for line in lines), lines)
        self.set_mode("ok")
        self.wait_for(lambda: watch.read_rung(self.sdir, "navigator") == 1)
        settled = len(self.calls())
        time.sleep(0.1)
        self.assertEqual(len(self.calls()), settled)
        self.finish(thread)

    def test_error_json_with_exit_0_is_a_failure(self):
        self.set_conversation("conv-1")
        self.set_mode("error-json")
        self.deliver()
        thread, _, _ = self.serve_in_thread()
        self.wait_for(lambda: len(self.calls()) >= 2)
        self.assertEqual(watch.read_rung(self.sdir, "navigator"), 0)
        self.finish(thread)

    def test_missing_executable_is_a_failure(self):
        os.environ["ANTIGRAVITY_AGENTAPI_EXE"] = os.path.join(self.tmp, "no-such-agy")
        self.set_conversation("conv-1")
        self.deliver()
        thread, _, lines = self.serve_in_thread()
        self.wait_for(lambda: any("failed" in line for line in lines))
        self.assertEqual(watch.read_rung(self.sdir, "navigator"), 0)
        self.finish(thread)

    def test_retry_waits_for_the_retry_interval(self):
        self.set_conversation("conv-1")
        self.set_mode("fail")
        self.deliver()
        thread, _, _ = self.serve_in_thread(retry_interval=60)
        self.wait_for(lambda: len(self.calls()) == 1)
        time.sleep(0.1)
        self.assertEqual(len(self.calls()), 1)
        self.finish(thread)

    def test_send_timeout_is_a_failure(self):
        def slow(*a, **kw):
            raise subprocess.TimeoutExpired(a[0], kw["timeout"])

        self.assertIn("timed out", watch.send_doorbell(self.exe, "c", "x", {}, run=slow))

    def test_failure_reason_falls_back_to_stderr(self):
        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 3, stdout="not json\n", stderr="\nLS refused the call\n")

        self.assertEqual(watch.send_doorbell(self.exe, "c", "x", {}, run=run), "exit 3: LS refused the call")

    def test_plain_text_stdout_with_exit_0_is_success(self):
        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, stdout="sent\n", stderr="")

        self.assertIsNone(watch.send_doorbell(self.exe, "c", "x", {}, run=run))


class ExitTest(AgyWatchTestCase):
    def test_ls_gone_exits_and_releases_the_pidfile(self):
        thread, result, _ = self.serve_in_thread()
        self.wait_for(lambda: os.path.exists(self.pidfile()))
        self.ls.close()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["reason"], "agy language server gone")
        self.assertFalse(os.path.exists(self.pidfile()))
        self.assertEqual(self.events("watcher_exited")[-1]["reason"], "agy language server gone")

    def test_localhost_address_reaches_an_ipv4_listener(self):
        self.assertTrue(watch.ls_alive("localhost:%d" % self.ls.port))
        self.ls.close()
        self.assertFalse(watch.ls_alive("localhost:%d" % self.ls.port))

    def test_ls_is_checked_every_ls_interval_not_every_poll(self):
        now = [0.0]
        checks = []

        def sleep(seconds):
            now[0] += seconds
            if now[0] >= 12:
                self.mark_ended()

        def alive(address):
            checks.append(now[0])
            return True

        with mock.patch.object(watch, "ls_alive", alive):
            reason = watch.serve(
                self.meta, "navigator", self.address, interval=1, ls_interval=5,
                clock=lambda: now[0], sleep=sleep, log=lambda line: None,
            )
        self.assertEqual(reason, "session ended")
        self.assertEqual(checks, [0, 5, 10])

    def test_session_end_exits(self):
        thread, result, _ = self.serve_in_thread()
        self.wait_for(lambda: self.events("watcher_started"))
        self.run_cli("--session", self.dir, "done")
        thread.join(5)
        self.assertEqual(result["reason"], "session ended")
        self.assertFalse(os.path.exists(self.pidfile()))

    def test_events_logged(self):
        thread, _, _ = self.serve_in_thread()
        self.wait_for(lambda: self.events("watcher_started"))
        self.finish(thread)
        started, exited = self.events("watcher_started")[0], self.events("watcher_exited")[0]
        self.assertEqual((started["role"], started["peer"], started["pid"]), ("navigator", "agy", os.getpid()))
        self.assertEqual((exited["pid"], exited["reason"]), (os.getpid(), "session ended"))


class SupersededRingTest(AgyWatchTestCase):
    def test_ring_by_a_superseded_watcher_leaves_the_marker_to_its_successor(self):
        self.set_conversation("conv-1")
        self.deliver()
        with mock.patch.object(watch, "send_doorbell", return_value=None), mock.patch.object(
            watch, "superseded", return_value=True
        ):
            self.assertTrue(watch._ring(self.sdir, "navigator", 1001, 1, {}, lambda line: None))
        self.assertEqual(watch.read_rung(self.sdir, "navigator"), 0)


class UnparseableAddressTest(AgyWatchTestCase):
    def test_unparseable_ls_address_is_not_a_gone_server(self):
        thread, result, lines = self.serve_in_thread(address="ls.sock")
        self.wait_for(lambda: self.events("watcher_started"))
        time.sleep(0.1)
        self.assertTrue(thread.is_alive())
        self.assertIn("cannot parse ANTIGRAVITY_LS_ADDRESS 'ls.sock'; not checking the language server", lines)
        self.finish(thread)
        self.assertEqual(result["reason"], "session ended")


class SingleInstanceTest(AgyWatchTestCase):
    def test_live_watcher_with_same_ls_address_keeps_running(self):
        other = self.live_process()
        self.write_pidfile({"pid": other, "ls_address": self.address})
        seen = []
        reason = watch.serve(self.meta, "navigator", self.address, ready=lambda *a: seen.append(a), log=lambda l: None)
        self.assertIsNone(reason)
        self.assertEqual(seen, [("running", other)])
        self.assertEqual(session.read_watcher(self.sdir, "navigator")["pid"], other)
        self.assertEqual(self.events("watcher_started"), [])

    def test_different_ls_address_takes_over(self):
        other = self.live_process()
        self.write_pidfile({"pid": other, "ls_address": "localhost:1"})
        thread, _, _ = self.serve_in_thread()
        self.wait_for(lambda: (session.read_watcher(self.sdir, "navigator") or {}).get("pid") == os.getpid())
        self.assertEqual(session.read_watcher(self.sdir, "navigator")["ls_address"], self.address)
        self.finish(thread)

    def test_dead_unreadable_and_claude_pidfiles_are_taken_over(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        live = self.live_process()
        for record in ({"pid": dead.pid, "ls_address": self.address}, "%d" % live, {"pid": live, "ls_address": None}):
            self.write_pidfile(record)
            self.assertIsNone(watch.claim_agy_pidfile(self.sdir, "navigator", self.address, os.getpid()))
            self.assertEqual(session.read_watcher(self.sdir, "navigator"), {"pid": os.getpid(), "ls_address": self.address})

    def test_old_watcher_sees_a_foreign_pidfile_and_exits(self):
        thread, result, _ = self.serve_in_thread()
        self.wait_for(lambda: os.path.exists(self.pidfile()))
        newer = self.live_process()
        self.assertIsNone(watch.claim_agy_pidfile(self.sdir, "navigator", "localhost:2", newer))
        thread.join(5)
        self.assertEqual(result["reason"], "superseded")
        self.assertEqual(session.read_watcher(self.sdir, "navigator"), {"pid": newer, "ls_address": "localhost:2"})


class DetachUnitTest(AgyWatchTestCase):
    """`watch.detach` itself, with a stand-in for the watcher.

    Run in a fresh interpreter: forking the multi-threaded test runner could deadlock the child.
    """

    STARTS = {
        "notify": "lambda notify: notify('started 7') or 0",
        "silent": "lambda notify: 0",
        "raises": "lambda notify: (_ for _ in ()).throw(RuntimeError('cannot claim the pidfile'))",
        "sleeps": "lambda notify: time.sleep(2) or 0",
    }

    def detach_with(self, start, timeout):
        log = os.path.join(self.tmp, "detach.log")
        script = (
            "import json, sys, time\n"
            "from tandem import watch\n"
            "t = time.monotonic()\n"
            "out = watch.detach(sys.argv[1], %s, timeout=float(sys.argv[2]))\n"
            "print(json.dumps([out, time.monotonic() - t]))\n" % self.STARTS[start]
        )
        proc = subprocess.run(
            [sys.executable, "-c", script, log, str(timeout)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(watch.__file__))),
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out, elapsed = json.loads(proc.stdout)
        return out, elapsed, log

    def test_notified_text_is_returned(self):
        self.assertEqual(self.detach_with("notify", 5)[0], "started 7")

    def test_child_that_exits_without_notifying(self):
        out, elapsed, _ = self.detach_with("silent", 5)
        self.assertEqual(out, "")
        self.assertLess(elapsed, 4)

    def test_child_that_raises(self):
        out, _, log = self.detach_with("raises", 5)
        self.assertEqual(out, "")
        with open(log) as f:
            self.assertIn("RuntimeError: cannot claim the pidfile", f.read())

    def test_child_silent_past_the_timeout(self):
        out, elapsed, _ = self.detach_with("sleeps", 0.2)
        self.assertEqual(out, "")
        self.assertLess(elapsed, 1.5)


class DetachTest(AgyWatchTestCase):
    def test_detach_returns_at_once_and_the_child_writes_its_pidfile(self):
        os.environ["ANTIGRAVITY_LS_ADDRESS"] = "localhost:%d" % self.ls.port
        proc, elapsed = self.detach()
        pid = self.started_pid(proc)
        self.assertLess(elapsed, 5)
        self.assertEqual(proc.stderr, "")
        self.assertIn(watch.watch_log_path(self.sdir, "navigator"), proc.stdout)
        self.assertTrue(session.pid_alive(pid))
        self.assertNotEqual(os.getsid(pid), os.getsid(0))
        self.assertEqual(
            session.read_watcher(self.sdir, "navigator"), {"pid": pid, "ls_address": "localhost:%d" % self.ls.port}
        )
        self.assertEqual(self.events("watcher_started")[0]["pid"], pid)

    def test_detached_watcher_rings_then_exits_when_ls_goes(self):
        self.set_conversation("conv-1")
        proc, _ = self.detach()
        pid = self.started_pid(proc)
        self.deliver(body="SECRET body text")
        self.wait_for(lambda: watch.read_rung(self.sdir, "navigator") == 1)
        self.assertEqual(self.calls()[0]["argv"], ["agentapi", "send-message", "conv-1", DOORBELL])
        self.assertEqual(self.calls()[0]["ls"], self.address)

        self.ls.close()
        self.wait_for(lambda: not self.watcher_processes())
        self.assertFalse(os.path.exists(self.pidfile()))
        exited = self.events("watcher_exited")[0]
        self.assertEqual((exited["pid"], exited["reason"]), (pid, "agy language server gone"))
        with open(watch.watch_log_path(self.sdir, "navigator")) as f:
            text = f.read()
        self.assertIn("rang conv-1 for #0001", text)
        self.assertIn("exiting: agy language server gone", text)
        self.assertNotIn("SECRET", text)

    def test_already_running_with_same_ls_address(self):
        # Found by the detached child's own claim under the lock, not by a check in the parent.
        other = self.live_process()
        self.write_pidfile({"pid": other, "ls_address": self.address})
        proc, _ = self.detach()
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(proc.stdout, "tandem watch: already running for navigator (pid %d)\n" % other)
        self.assertEqual(session.read_watcher(self.sdir, "navigator")["pid"], other)
        self.assertEqual(self.watcher_processes(), [])

    def test_watcher_that_never_reports_is_an_error(self):
        with mock.patch.object(watch, "detach", return_value=""):
            code, out, err = self.run_cli("--session", self.dir, "watch", "--as", "navigator", "--detach")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("watcher did not start; see %s" % watch.watch_log_path(self.sdir, "navigator"), err)

    def test_second_detach_reports_the_running_watcher(self):
        pid = self.started_pid(self.detach()[0])
        again, _ = self.detach()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout, "tandem watch: already running for navigator (pid %d)\n" % pid)
        self.assertEqual(self.watcher_processes(), [pid])

    def test_sigterm_releases_the_pidfile(self):
        pid = self.started_pid(self.detach()[0])
        os.kill(pid, signal.SIGTERM)
        self.wait_for(lambda: not self.watcher_processes())
        self.assertFalse(os.path.exists(self.pidfile()))
        self.assertEqual(self.events("watcher_exited")[0]["reason"], "terminated")

    def test_missing_env_through_the_shim(self):
        del os.environ["ANTIGRAVITY_LS_ADDRESS"]
        proc, _ = self.detach()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("must be started by agy itself", proc.stderr)
        self.assertEqual(self.watcher_processes(), [])
