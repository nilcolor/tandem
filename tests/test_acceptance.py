import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

from tandem import launch_agy, launch_claude, session, watch
from tests.helpers import SHIM, TandemTestCase
from tests.test_agy_selfcheck import FAKE_AGY
from tests.test_watch_agy import FAKE_AGENTAPI, FakeLS

FAKE_CLAUDE = """#!%s
import json, os, sys
with open(os.environ["FAKE_CLAUDE_OUT"], "w") as f:
    json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "pid": os.getpid(),
               "session": os.environ.get("TANDEM_SESSION"), "role": os.environ.get("TANDEM_ROLE")}, f)
"""

BYPASS = {"decision": "allow", "overwrite": {"BypassSandbox": True}}
DRIVER_BELL = "tandem doorbell: new mail for driver. Run `tandem recv`."
NAVIGATOR_BELL = "tandem doorbell: new mail for navigator. Run `tandem recv`."
# The real detach command plus the hidden test knobs, so the detached watcher reacts in milliseconds.
DETACH = "tandem watch --as navigator --detach --poll-interval 0.02 --ls-check-interval 0.05"


class AcceptanceTestCase(TandemTestCase):
    """One session driven the way the two harnesses drive it.

    Everything goes through the shim as a child process: `tandem` and the
    generated hook commands run under `sh -c` with `tandem`, `claude` and
    `agy` on PATH, so the fakes stand only where the harnesses would.
    """

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("apix")
        with open(os.path.join(self.repo, "go.mod"), "w") as f:
            f.write("module apix\n")
        self.git(self.repo, "add", "go.mod")
        self.git(self.repo, "commit", "-q", "-m", "go module")

        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)
        # The shim's `env python3` must find the interpreter running this suite.
        os.symlink(sys.executable, os.path.join(self.bindir, "python3"))
        os.symlink(SHIM, os.path.join(self.bindir, "tandem"))
        os.symlink(shutil.which("git"), os.path.join(self.bindir, "git"))
        for name, text in (("claude", FAKE_CLAUDE), ("agy", FAKE_AGY)):
            path = os.path.join(self.bindir, name)
            with open(path, "w") as f:
                f.write(text % sys.executable)
            os.chmod(path, 0o755)
        os.environ["PATH"] = os.pathsep.join([self.bindir, "/usr/bin", "/bin"])
        os.environ["FAKE_CLAUDE_OUT"] = os.path.join(self.tmp, "claude-exec.json")
        os.environ["FAKE_AGY_OUT"] = os.path.join(self.tmp, "agy-exec.json")

        self.state = os.path.join(self.tmp, "agentapi")
        os.makedirs(self.state)
        self.agentapi = os.path.join(self.state, "agentapi")
        with open(self.agentapi, "w") as f:
            f.write(FAKE_AGENTAPI % {"python": sys.executable, "state": self.state})
        os.chmod(self.agentapi, 0o755)

        self.ls = self.new_ls()
        # A unique session name lets teardown find this test's watchers by command line.
        self.name = "e2e-" + uuid.uuid4().hex[:10]
        self.waiters = []
        # The conversation agy is on; every hook agy runs reports it.
        self.conv = "conv-1"
        # agy's watcher finds its session through TANDEM_SESSION, so its command line doesn't name it.
        self.agy_watchers = set()
        self.addCleanup(self.reap)

    # harness stand-ins

    def new_ls(self):
        ls = FakeLS()
        self.addCleanup(ls.close)
        return ls

    def address(self, ls):
        return "127.0.0.1:%d" % ls.port

    def sh(self, command, env=None, stdin="", cwd=None):
        return subprocess.run(
            ["/bin/sh", "-c", command],
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            cwd=cwd or self.repo,
            env=env or dict(os.environ),
        )

    def ok(self, command, env=None, stdin="", cwd=None):
        proc = self.sh(command, env, stdin, cwd)
        self.assertEqual(proc.returncode, 0, "%s\n%s%s" % (command, proc.stdout, proc.stderr))
        return proc.stdout

    def init(self, *extra):
        sdir = self.ok("tandem init %s %s" % (self.name, " ".join(extra))).strip()
        self.dir = os.path.basename(sdir)
        self.sdir = sdir
        return sdir

    def launch_claude(self, *extra):
        self.ok("tandem claude " + " ".join(extra))
        with open(os.environ["FAKE_CLAUDE_OUT"]) as f:
            rec = json.load(f)
        self.claude_env = dict(os.environ, TANDEM_SESSION=rec["session"], TANDEM_ROLE=rec["role"])
        return rec

    def launch_agy(self, ls, *extra):
        self.ok("tandem agy " + " ".join(extra))
        with open(os.environ["FAKE_AGY_OUT"]) as f:
            rec = json.load(f)
        # What agy's run_command children (and, we assume, its hooks) get from agy.
        self.agy_env = dict(
            os.environ,
            TANDEM_SESSION=rec["session"],
            TANDEM_ROLE=rec["role"],
            ANTIGRAVITY_LS_ADDRESS=self.address(ls),
            ANTIGRAVITY_CSRF_TOKEN="csrf-token",
            ANTIGRAVITY_AGENTAPI_EXE=self.agentapi,
        )
        return rec

    def claude_settings(self):
        with open(launch_claude.settings_path(self.sdir)) as f:
            return json.load(f)

    def claude_hook_command(self, event):
        return self.claude_settings()["hooks"][event][0]["hooks"][0]["command"]

    def claude_prompt(self, prompt):
        """Claude's UserPromptSubmit hook, run as Claude runs it."""
        proc = self.sh(self.claude_hook_command("UserPromptSubmit"), self.claude_env, json.dumps({"prompt": prompt}))
        self.assertEqual((proc.returncode, proc.stdout), (0, ""), proc.stderr)

    def arm_claude(self):
        """Claude ended a turn: its Stop hook starts the asyncRewake waiter from settings.json."""
        proc = subprocess.Popen(
            ["/bin/sh", "-c", self.claude_hook_command("Stop")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            cwd=self.repo,
            env=self.claude_env,
            start_new_session=True,
        )
        self.waiters.append(proc)
        # With mail already waiting it rings and exits before anyone sees its pidfile.
        self.wait_for(lambda: proc.poll() is not None or session.read_watcher(self.sdir, "driver") is not None)
        return proc

    def wake(self, proc, timeout=10):
        """The waiter's exit code and stderr, as asyncRewake sees them."""
        _, err = proc.communicate(timeout=timeout)
        return proc.returncode, err

    def agy_hook(self, event, payload, env=None):
        spec = None
        with open(launch_agy.hooks_path(self.sdir)) as f:
            spec = json.load(f)["tandem"][event]
        command = spec[0]["hooks"][0]["command"] if event == "PreToolUse" else spec[0]["command"]
        base = {"conversationId": self.conv, "workspacePaths": [self.repo], "cwd": self.repo, "hookEventName": event}
        base.update(payload)
        proc = self.sh(command, env or self.agy_env, json.dumps(base), cwd=launch_agy.agents_dir(self.sdir))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def pretool(self, command, **kw):
        return self.agy_hook("PreToolUse", {"toolCall": {"name": "run_command", "args": {"CommandLine": command}}}, **kw)

    def agy_stop(self, **kw):
        return self.agy_hook("Stop", {"terminationReason": "NO_TOOL_CALL", "fullyIdle": True}, **kw)

    def agy_run(self, command, env=None):
        """agy runs `command` as a run_command tool call; the hook must let it bypass the sandbox."""
        self.assertEqual(self.pretool(command, env=env), BYPASS, command)
        return self.ok(command, env or self.agy_env)

    def start_agy_watcher(self, env=None):
        out = self.agy_run(DETACH, env)
        match = re.search(r"started for navigator \(pid (\d+)\)", out)
        self.assertIsNotNone(match, out)
        pid = int(match.group(1))
        self.agy_watchers.add(pid)
        return pid

    def calls(self):
        try:
            with open(os.path.join(self.state, "calls.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    # teardown

    def watcher_processes(self):
        if not getattr(self, "dir", None):
            return []
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="], stdout=subprocess.PIPE, universal_newlines=True, check=True
        ).stdout
        found = []
        for line in out.splitlines():
            pid, _, command = line.strip().partition(" ")
            pid = int(pid)
            if " watch " in command and pid != os.getpid() and (self.dir in command or pid in self.agy_watchers):
                found.append(pid)
        return found

    def reap(self):
        for proc in self.waiters:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
        for pid in self.watcher_processes():
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
        self.assertEqual(leftover, [], "stray tandem watch processes")

    def setup_pair(self):
        """`tandem init`, `tandem claude`, `tandem agy`, and agy's SessionStart starting its watcher."""
        self.init("--task", "'Add retries to the HTTP client'")
        self.launch_claude()
        self.launch_agy(self.ls)
        self.agy_hook("SessionStart", {})
        return self.start_agy_watcher()


class EndToEndTest(AcceptanceTestCase):
    def test_task_from_init_through_review_to_done(self):
        sdir = self.init("--task", "'Add retries to the HTTP client'")
        self.assertEqual(os.path.dirname(sdir), os.path.join(self.tandem_home, "sessions"))
        meta = session.load_session(self.dir)
        self.assertEqual(meta["roles"], {"driver": "claude", "navigator": "agy"})
        self.assertEqual(meta["test_cmds"], ["go test"])

        claude = self.launch_claude()
        self.assertEqual((claude["session"], claude["role"], claude["cwd"]), (self.dir, "driver", self.repo))
        self.assertEqual(claude["argv"][claude["argv"].index("--settings") + 1], launch_claude.settings_path(sdir))
        agy = self.launch_agy(self.ls)
        self.assertEqual((agy["session"], agy["role"]), (self.dir, "navigator"))
        with open(launch_agy.profile_path(sdir)) as f:
            profile = json.load(f)
        self.assertEqual(profile["toolPermission"], "proceed-in-sandbox")
        self.assertIn("unsandboxed(go test)", profile["permissions"]["allow"])

        # Launcher first prompts are not human input; what the user types is.
        self.claude_prompt(claude["argv"][-1])
        self.claude_prompt("Retries: at most 3, with backoff.")
        agy_first = [a for a in agy["argv"] if a.startswith("--prompt-interactive=")][0].partition("=")[2]
        self.assertEqual(self.agy_hook("PreInvocation", {"lastUserInput": agy_first}), {})

        started = self.agy_hook("SessionStart", {})
        message = started["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("you are the navigator in tandem session %s" % self.dir, message)
        self.assertIn("running exactly: tandem watch --as navigator --detach", message)
        watcher = self.start_agy_watcher()
        self.assertEqual(self.agy_stop(), {})

        waiter = self.arm_claude()
        sent = self.ok("tandem send --to navigator --type plan 'Plan: wrap Do() in a retry loop.'", self.claude_env)
        self.assertEqual(sent, "sent 0001 to navigator\n")
        self.wait_for(lambda: len(self.calls()) == 1)
        self.assertEqual(
            self.calls(),
            [{"argv": ["agentapi", "send-message", "conv-1", NAVIGATOR_BELL], "ls": self.address(self.ls)}],
        )
        self.assertEqual(self.agy_hook("PreInvocation", {"lastUserInput": NAVIGATOR_BELL}), {})
        got = self.agy_run("tandem recv")
        self.assertTrue(got.startswith("#0001 driver → navigator  plan  expects reply\n"), got)
        self.assertIn("Plan: wrap Do() in a retry loop.", got)

        review = "tandem send --to driver --type review --reply-to 1 'APPROVE\nrisks: none\nobjection: none'"
        self.assertEqual(self.agy_run(review), "sent 0002 to driver\n")
        self.assertEqual(self.wake(waiter), (2, DRIVER_BELL + "\n"))
        got = self.ok("tandem recv", self.claude_env)
        self.assertTrue(got.startswith("#0002 navigator → driver  review  reply to #0001\n"), got)

        self.agy_run("tandem send --to human --type escalate 'Max retries: 3 or 5?'")
        out = self.ok("tandem human", env=dict(os.environ), stdin="@navigator 3\n/quit\n")
        self.assertIn("#0003 navigator → human  escalate  expects reply", out)
        self.assertIn("sent #0004 to navigator (answer, reply to #0003)", out)
        self.wait_for(lambda: len(self.calls()) == 2)
        self.assertEqual(self.agy_run("tandem recv").splitlines()[-1], "3")

        waiter = self.arm_claude()
        with open(os.path.join(sdir, "decisions.md"), "w") as f:
            f.write("- retries capped at 3 (user)\n")
        self.ok("tandem send --to human --type done 'Retries added, capped at 3.'", self.claude_env)
        summary = self.ok("tandem done", self.claude_env).strip()
        self.assertEqual(summary, os.path.join(sdir, "summary.md"))
        self.assertEqual(self.wake(waiter), (0, ""))
        self.wait_for(lambda: not self.watcher_processes())
        self.assertEqual(self.events("watcher_exited")[-1]["reason"], "session ended")
        self.assertNotIn(watcher, self.watcher_processes())

        with open(summary) as f:
            text = f.read()
        self.assertIn("## Messages (5)", text)
        self.assertIn("## Human input in consoles (1)", text)
        self.assertIn("> Retries: at most 3, with backoff.", text)
        self.assertIn("Retries added, capped at 3.", text)
        self.assertIn("- retries capped at 3 (user)", text)
        self.assertNotIn("tandem:", text.split("## Human input")[1].split("## Wrap-up")[0])
        self.assertIsNone(session.read_pointer(self.repo))
        self.assertEqual(len(self.calls()), 2, "a ring for mail nobody sent")
        # Nothing was written into the repo or the user's own harness config.
        self.assertEqual(self.git(self.repo, "status", "--porcelain", "--ignored"), "")
        # Only the harness config dirs: Apple's python 3.9 puts its bytecode cache under ~/Library.
        for config in (".claude", ".gemini", ".tandem"):
            self.assertFalse(os.path.exists(os.path.join(self.home, config)), config)


class EdgeCaseTest(AcceptanceTestCase):
    def test_ignored_message_is_not_rung_again(self):
        self.setup_pair()
        once = "tandem watch --once --deadline 1"

        self.ok("tandem send --to driver --type question 'Which client?'", self.agy_env)
        self.assertEqual(self.wake(self.arm_claude()), (2, DRIVER_BELL + "\n"))
        # Claude ignored it (no recv): its next waiter sits out the deadline instead of ringing.
        self.assertEqual(self.sh(once, self.claude_env).returncode, 0)
        self.ok("tandem send --to driver --type question 'And which timeout?'", self.agy_env)
        self.assertEqual(self.wake(self.arm_claude()), (2, DRIVER_BELL + "\n"))

        self.ok("tandem send --to navigator --type plan 'Plan v1'", self.claude_env)
        self.wait_for(lambda: len(self.calls()) == 1)
        time.sleep(0.3)
        self.assertEqual(len(self.calls()), 1)
        # agy went idle without reading it: Stop has nothing new to say.
        self.assertEqual(self.agy_stop(), {})
        self.ok("tandem send --to navigator --type plan 'Plan v2'", self.claude_env)
        self.wait_for(lambda: len(self.calls()) == 2)

    def test_claude_idle_past_the_deadline_rearms_on_the_next_turn(self):
        self.setup_pair()
        # The waiter gave up at its deadline, so nothing waits while mail arrives.
        proc = self.sh("tandem watch --once --deadline 0.2", self.claude_env)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.ok("tandem send --to driver --type review 'APPROVE'", self.agy_env)
        # The user's next prompt gives Claude a turn; its Stop hook arms a new waiter, which rings at once.
        self.claude_prompt("anything new?")
        start = time.monotonic()
        self.assertEqual(self.wake(self.arm_claude()), (2, DRIVER_BELL + "\n"))
        self.assertLess(time.monotonic() - start, 5)

    def test_agy_restart_starts_a_new_watcher_on_the_new_language_server(self):
        old = self.setup_pair()
        self.ls.close()
        self.wait_for(lambda: old not in self.watcher_processes())
        self.assertEqual(self.events("watcher_exited")[-1]["reason"], "agy language server gone")

        self.ok("tandem send --to navigator --type plan 'Plan sent while agy was down'", self.claude_env)
        new_ls = self.new_ls()
        self.launch_agy(new_ls)
        self.conv = "conv-after-restart"
        started = self.agy_hook("SessionStart", {})
        message = started["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("running exactly: tandem watch --as navigator --detach", message)
        self.assertIn("1 message(s) pending", message)
        new = self.start_agy_watcher()
        self.assertNotEqual(new, old)
        time.sleep(0.3)
        self.assertEqual(self.calls(), [], "mail SessionStart announced was rung again")

        self.ok("tandem send --to navigator --type plan 'Plan v2'", self.claude_env)
        self.wait_for(lambda: len(self.calls()) == 1)
        self.assertEqual(self.calls()[0]["ls"], self.address(new_ls))
        self.assertEqual(self.calls()[0]["argv"][2], "conv-after-restart")

    def test_watchdog_restarts_a_watcher_that_died(self):
        first = self.setup_pair()
        os.kill(first, signal.SIGTERM)
        self.wait_for(lambda: first not in self.watcher_processes())
        self.ok("tandem send --to navigator --type plan 'Plan sent while nothing watched'", self.claude_env)
        # Past the watchdog's backoff since SessionStart's nudge.
        session.write_text_atomic(os.path.join(self.sdir, "peers", "agy", "watchdog"), "0\n")
        stop = self.agy_stop()
        self.assertEqual(stop["decision"], "continue")
        self.assertIn("running exactly: tandem watch --as navigator --detach", stop["reason"])
        self.assertIn("new mail for navigator", stop["reason"])
        # Asked again right away, it holds back the nudge so a watcher that can't start can't loop agy.
        self.assertEqual(self.agy_stop(), {})
        self.start_agy_watcher()
        time.sleep(0.3)
        self.assertEqual(self.calls(), [], "mail the Stop hook announced was rung again")

    def test_agy_restart_before_the_old_watcher_notices(self):
        self.init()
        self.launch_claude()
        self.launch_agy(self.ls)
        slow = DETACH.replace("--ls-check-interval 0.05", "--ls-check-interval 60")
        self.assertEqual(self.pretool(slow), BYPASS)
        old = int(re.search(r"pid (\d+)", self.ok(slow, self.agy_env)).group(1))
        self.agy_watchers.add(old)

        new_ls = self.new_ls()
        self.launch_agy(new_ls)
        started = self.agy_hook("SessionStart", {})
        self.assertIn("watcher is not running", started["injectSteps"][0]["systemMessage"]["systemMessage"])
        new = self.start_agy_watcher()
        self.wait_for(lambda: old not in self.watcher_processes())
        self.assertIn("superseded", [e["reason"] for e in self.events("watcher_exited") if e["pid"] == old])

        self.ok("tandem send --to navigator --type plan 'Plan'", self.claude_env)
        self.wait_for(lambda: len(self.calls()) == 1)
        self.assertEqual(self.calls()[0]["ls"], self.address(new_ls))
        self.assertEqual(self.watcher_processes(), [new])

    def test_agy_new_conversation_gets_the_next_ring(self):
        self.setup_pair()
        # README (verified): after `/new` no hook runs until the user's first message, so a ring
        # in that window still goes to the old conversation.
        self.ok("tandem send --to navigator --type plan 'Plan'", self.claude_env)
        self.wait_for(lambda: len(self.calls()) == 1)
        self.conv = "conv-2"
        started = self.agy_hook("SessionStart", {})
        self.assertIn("1 message(s) pending", started["injectSteps"][0]["systemMessage"]["systemMessage"])
        self.assertEqual(self.agy_hook("PreInvocation", {"lastUserInput": "hi"}), {})
        self.ok("tandem send --to navigator --type question 'Still there?'", self.claude_env)
        self.wait_for(lambda: len(self.calls()) == 2)
        self.assertEqual([c["argv"][2] for c in self.calls()], ["conv-1", "conv-2"])

    def test_ended_session_stops_both_watchers(self):
        agy_watcher = self.setup_pair()
        waiter = self.arm_claude()
        self.assertIn(agy_watcher, self.watcher_processes())
        self.ok("tandem done", self.claude_env)
        self.assertEqual(self.wake(waiter), (0, ""))
        self.wait_for(lambda: not self.watcher_processes())
        self.assertEqual(
            [(e["pid"], e["reason"]) for e in self.events("watcher_exited")], [(agy_watcher, "session ended")]
        )
        for role in ("driver", "navigator"):
            self.assertFalse(os.path.exists(session.watcher_pidfile(self.sdir, role)), role)
        # Nothing restarts them: hooks are no-ops and a new watcher refuses an ended session.
        self.assertEqual(self.agy_stop(), {})
        self.assertIn("has ended", self.ok(DETACH, self.agy_env))
        self.assertEqual(self.sh(self.claude_hook_command("Stop"), self.claude_env).returncode, 0)
        self.assertEqual(self.watcher_processes(), [])
