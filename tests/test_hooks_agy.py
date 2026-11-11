import json
import os
import shutil
import subprocess
import sys
from unittest import mock

from tandem import hooks, maildir, session, watch
from tests.helpers import TandemTestCase

CONV = "ec33ebf9-0cba-4100-8142-c61503f6c587"
LS = "127.0.0.1:45123"
ASK = {"decision": "ask"}
BYPASS = {"decision": "allow", "overwrite": {"BypassSandbox": True}}


def common(event, **extra):
    data = {
        "conversationId": CONV,
        "workspacePaths": ["/tmp/repo"],
        "transcriptPath": "/tmp/t.jsonl",
        "modelName": "auto",
        "lastUserInput": "",
        "hookEventName": event,
        "cwd": "/tmp/repo",
    }
    data.update(extra)
    return data


def pretool(command, name="run_command", **extra):
    data = common("PreToolUse", toolCall={"id": "toolu_01", "name": name, "args": {"CommandLine": command}}, stepIdx=3)
    data.update(extra)
    return json.dumps(data)


def stop(reason="NO_TOOL_CALL", idle=True, **extra):
    data = common("Stop", executionNum=1, terminationReason=reason, **extra)
    if idle is not None:
        data["fullyIdle"] = idle
    return json.dumps(data)


class AgyHookTestCase(TandemTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("apix")
        with open(os.path.join(self.repo, "go.mod"), "w") as f:
            f.write("module apix\n")
        self.meta = session.init_session(cwd=self.repo, name="add-retry")
        self.dir = self.meta["dir"]
        self.sdir = session.session_path(self.dir)
        os.chdir(self.repo)

    def hook(self, event, stdin, *pre):
        """(exit code, decoded stdout JSON, stderr)."""
        code, out, err = self.run_cli(*(pre or ("--session", self.dir)), "hook", event, stdin=stdin)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("\n"), 1, out)
        return json.loads(out), err

    def out(self, event, stdin, *pre):
        return self.hook(event, stdin, *pre)[0]

    def update(self, **fields):
        meta = session.load_session(self.dir)
        meta.update(fields)
        session.save_session(meta)

    def agy_file(self, name):
        try:
            with open(os.path.join(self.sdir, "peers", "agy", name), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def hooks_log(self):
        try:
            with open(hooks.hooks_log_path(self.sdir, "agy"), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def consoles(self):
        return [r for r in session.read_log(self.sdir) if r.get("kind") == "console"]

    def send(self, to="navigator", body="please review", sender="driver"):
        return maildir.deliver(self.meta, maildir.build_envelope(sender, to, body))

    def dead_pid(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def write_watcher(self, role="navigator", pid=None, ls_address=LS, raw=None):
        os.makedirs(os.path.join(self.sdir, "peers", role), exist_ok=True)
        text = raw if raw is not None else json.dumps({"pid": pid or os.getpid(), "ls_address": ls_address})
        with open(session.watcher_pidfile(self.sdir, role), "w") as f:
            f.write(text)


class PretoolBypassTest(AgyHookTestCase):
    def test_detected_test_prefix(self):
        self.assertEqual(self.out("agy-pretool", pretool("go test ./...")), BYPASS)

    def test_each_configured_test_prefix(self):
        self.update(test_cmds=["dip rspec", "bundle exec rspec", "make check"])
        for cmd in ("dip rspec spec/a_spec.rb", "bundle exec rspec", "make check V=1"):
            self.assertEqual(self.out("agy-pretool", pretool(cmd)), BYPASS, cmd)
        self.assertEqual(self.out("agy-pretool", pretool("go test ./...")), ASK)

    def test_tandem_commands(self):
        for cmd in ("tandem recv", "tandem status", "tandem watch --as navigator --detach", "tandem"):
            self.assertEqual(self.out("agy-pretool", pretool(cmd)), BYPASS, cmd)

    def test_tandem_without_test_cmds(self):
        self.update(test_cmds=[])
        self.assertEqual(self.out("agy-pretool", pretool("tandem recv")), BYPASS)
        self.assertEqual(self.out("agy-pretool", pretool("go test ./...")), ASK)

    def test_multi_line_single_quoted_send(self):
        cmd = (
            "tandem send --to driver --type review --reply-to 3 'CHANGES\n"
            "- retry.go:42: `ctx` is ignored; a cancelled call still sleeps > 1s\n"
            "- keep $(...) out of the loop && add a test | maybe two'"
        )
        self.assertEqual(self.out("agy-pretool", pretool(cmd)), BYPASS)

    def test_prefix_lookalikes(self):
        for cmd in ("tandemx status", "go testx", "gotest", "go", "echo tandem status", "./tandem status"):
            self.assertNotEqual(self.out("agy-pretool", pretool(cmd)), BYPASS, cmd)

    def test_compound_commands(self):
        for cmd in ("go test ./... && rm -rf x", "tandem status; touch f", "tandem log | head", "cd a && tandem recv"):
            self.assertNotEqual(self.out("agy-pretool", pretool(cmd)), BYPASS, cmd)

    def test_redirects_and_substitutions(self):
        for cmd in (
            "go test ./... > out.txt",
            "tandem send < body.md",
            "tandem send $(cat body.md)",
            "tandem send `cat body.md`",
            'tandem send "$BODY"',
            "tandem status 2>&1",
        ):
            self.assertNotEqual(self.out("agy-pretool", pretool(cmd)), BYPASS, cmd)

    def test_safe_reads_pass_through(self):
        for cmd in ("ls -la", "git diff HEAD~1", "cat go.mod", "tandem status | cat"):
            self.assertEqual(self.out("agy-pretool", pretool(cmd)), ASK, cmd)

    def test_never_emits_empty_object(self):
        for stdin in (pretool("ls"), pretool("rm x"), pretool("tandem recv"), "not json", "{}", "[]"):
            self.assertNotEqual(self.out("agy-pretool", stdin), {}, stdin)

    def test_non_run_command_tool(self):
        self.assertEqual(self.out("agy-pretool", pretool("rm -rf x", name="write_file")), ASK)
        self.assertEqual(self.hooks_log(), "")

    def test_bypass_applies_after_session_end(self):
        session.end_session(session.load_session(self.dir))
        self.assertEqual(self.out("agy-pretool", pretool("tandem status")), BYPASS)


class PretoolWriteGateTest(AgyHookTestCase):
    def gate(self, cmd):
        return self.out("agy-pretool", pretool(cmd))

    def assert_gated(self, cmd, why):
        out = self.gate(cmd)
        self.assertEqual(out["decision"], "force_ask", cmd)
        self.assertIn("(%s)" % why, out["reason"])
        self.assertNotIn("overwrite", out)

    def test_navigator_writes_force_ask(self):
        self.assert_gated("touch notes.txt", "touch")
        self.assert_gated("rm -rf build", "rm")
        self.assert_gated("echo x > f.go", "redirect > f.go")
        self.assert_gated("sed -i s/a/b/ main.go", "sed -i")
        self.assert_gated("git commit -am wip", "git commit")
        self.assert_gated("git checkout -- main.go", "git checkout")
        self.assert_gated("ls && mv a b", "mv")
        self.assert_gated("echo $(rm f)", "rm")

    def test_compound_with_bypass_prefix_is_gated(self):
        self.assert_gated("go test ./... && rm -rf x", "rm")
        self.assert_gated("tandem status > status.txt", "redirect > status.txt")

    def test_bypass_wins_over_gate_words_in_quoted_body(self):
        self.assertEqual(self.gate("tandem send --to driver 'run: rm -rf build > out; git commit'"), BYPASS)

    def test_reads_and_harmless_redirects_pass(self):
        for cmd in ("git status", "git log -5", "go vet ./... 2>&1", "ls >/dev/null 2>&1", "grep -rn 'rm -rf' ."):
            self.assertEqual(self.gate(cmd), ASK, cmd)

    def test_driver_is_not_gated(self):
        self.update(roles={"driver": "agy", "navigator": "claude"})
        for cmd in ("touch notes.txt", "git commit -am wip", "echo x > f"):
            self.assertEqual(self.gate(cmd), ASK, cmd)
        self.assertEqual(self.gate("tandem recv"), BYPASS)

    def test_role_read_from_session_not_env(self):
        os.environ["TANDEM_ROLE"] = "driver"
        self.assertEqual(self.gate("touch x")["decision"], "force_ask")

    def test_agy_without_role_is_gated(self):
        self.update(roles={"driver": "claude", "navigator": "someone"})
        self.assertEqual(self.gate("touch x")["decision"], "force_ask")


class PretoolInputTest(AgyHookTestCase):
    def test_malformed_stdin(self):
        for stdin in ("not json", "", "[1]", json.dumps(common("PreToolUse"))):
            self.assertEqual(self.out("agy-pretool", stdin), ASK, stdin)
        self.assertEqual(len(self.hooks_log().splitlines()), 4)
        self.assertIn("agy-pretool: HookInputError: no toolCall object", self.hooks_log())

    def test_missing_command_line(self):
        data = common("PreToolUse", toolCall={"id": "x", "name": "run_command", "args": {}})
        self.assertEqual(self.out("agy-pretool", json.dumps(data)), ASK)
        self.assertIn("without a CommandLine", self.hooks_log())
        data["toolCall"]["args"] = "ls"
        self.assertEqual(self.out("agy-pretool", json.dumps(data)), ASK)

    def test_unexpected_exception(self):
        with mock.patch("tandem.cmdline.bypass_prefix", side_effect=RuntimeError("boom")):
            self.assertEqual(self.out("agy-pretool", pretool("tandem recv")), ASK)
        self.assertIn("agy-pretool: RuntimeError: boom", self.hooks_log())

    def test_unknown_session(self):
        out, err = self.hook("agy-pretool", pretool("tandem recv"), "--session", "nope")
        self.assertEqual(out, ASK)
        self.assertIn("agy-pretool", err)

    def test_via_shim(self):
        proc = self.run_shim("--session", self.dir, "hook", "agy-pretool", stdin=pretool("tandem recv"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), BYPASS)


class ConversationIdTest(AgyHookTestCase):
    def stdin_for(self, event, conv):
        data = {
            "agy-pretool": json.loads(pretool("ls")),
            "agy-session-start": common("SessionStart"),
            "agy-preinvocation": common("PreInvocation", invocationNum=1),
            "agy-stop": json.loads(stop()),
        }[event]
        data["conversationId"] = conv
        return json.dumps(data)

    def test_every_hook_writes_it(self):
        for i, event in enumerate(("agy-pretool", "agy-session-start", "agy-preinvocation", "agy-stop")):
            conv = "conv-%d" % i
            self.out(event, self.stdin_for(event, conv))
            self.assertEqual(self.agy_file("conversation_id"), conv + "\n", event)
            self.assertEqual(watch.conversation_id(self.sdir, {}), conv)

    def test_atomic_write_leaves_no_temp_files(self):
        self.out("agy-pretool", self.stdin_for("agy-pretool", "conv-a"))
        self.out("agy-pretool", self.stdin_for("agy-pretool", "conv-b"))
        self.assertEqual(sorted(os.listdir(os.path.join(self.sdir, "peers", "agy"))), ["conversation_id"])

    def test_written_through_rename(self):
        with mock.patch.object(session, "write_text_atomic", wraps=session.write_text_atomic) as w:
            self.out("agy-pretool", self.stdin_for("agy-pretool", "conv-x"))
        w.assert_called_once_with(watch.conversation_path(self.sdir), "conv-x\n")

    def test_unchanged_id_not_rewritten(self):
        self.out("agy-pretool", self.stdin_for("agy-pretool", CONV))
        with mock.patch.object(session, "write_text_atomic") as w:
            self.out("agy-pretool", self.stdin_for("agy-pretool", CONV))
        w.assert_not_called()

    def test_missing_or_blank_id_keeps_the_old_one(self):
        self.out("agy-pretool", self.stdin_for("agy-pretool", CONV))
        for conv in ("", "  ", None, 7):
            self.out("agy-pretool", self.stdin_for("agy-pretool", conv))
        self.assertEqual(self.agy_file("conversation_id"), CONV + "\n")

    def test_write_failure_still_decides(self):
        with mock.patch.object(session, "write_text_atomic", side_effect=OSError("read-only")):
            self.assertEqual(self.out("agy-pretool", self.stdin_for("agy-pretool", "c")), ASK)
            self.assertEqual(self.out("agy-pretool", pretool("tandem recv", conversationId="c2")), BYPASS)
        self.assertIn("agy-pretool: OSError: read-only", self.hooks_log())

    def test_written_for_ended_session(self):
        session.end_session(session.load_session(self.dir))
        self.assertEqual(self.out("agy-session-start", self.stdin_for("agy-session-start", "after-end")), {})
        self.assertEqual(self.agy_file("conversation_id"), "after-end\n")


class SessionStartTest(AgyHookTestCase):
    def start(self, **extra):
        return self.out("agy-session-start", json.dumps(common("SessionStart", **extra)))

    def message(self, out):
        [step] = out["injectSteps"]
        return step["systemMessage"]["systemMessage"]

    def test_watcher_dead_and_no_mail(self):
        text = self.message(self.start())
        self.assertIn("you are the navigator in tandem session %s" % self.dir, text)
        self.assertIn("running exactly: tandem watch --as navigator --detach", text)
        self.assertNotIn("pending", text)
        self.assertIsNotNone(self.agy_file("watchdog"))

    def test_watcher_alive(self):
        self.write_watcher()
        text = self.message(self.start())
        self.assertIn("navigator", text)
        self.assertNotIn("tandem watch", text)
        self.assertIsNone(self.agy_file("watchdog"))

    def test_watcher_alive_for_this_ls(self):
        self.write_watcher()
        os.environ["ANTIGRAVITY_LS_ADDRESS"] = LS
        self.assertNotIn("tandem watch", self.message(self.start()))

    def test_watcher_serving_another_agy_counts_as_dead(self):
        self.write_watcher(ls_address="127.0.0.1:1")
        os.environ["ANTIGRAVITY_LS_ADDRESS"] = LS
        self.assertIn("tandem watch --as navigator --detach", self.message(self.start()))

    def test_dead_pid_bare_pid_and_garbage_are_dead(self):
        for raw in (json.dumps({"pid": self.dead_pid(), "ls_address": LS}), "%d\n" % os.getpid(), "garbage"):
            self.write_watcher(raw=raw)
            self.assertIn("tandem watch --as navigator --detach", self.message(self.start()), raw)

    def test_pending_mail(self):
        self.write_watcher()
        self.send()
        self.send()
        self.send(to="driver", sender="navigator")
        text = self.message(self.start())
        self.assertIn("2 message(s) pending: run `tandem recv`", text)
        # Announced here, so the watcher won't ring for the same mail.
        self.assertEqual(watch.read_rung(self.sdir, "navigator"), 2)
        self.assertIsNone(watch.unrung_id(self.sdir, "navigator"))

    def test_pending_already_rung_is_still_reported(self):
        self.send()
        with session.locked(self.sdir):
            watch.mark_rung(self.sdir, "navigator", 1)
        self.assertIn("1 message(s) pending", self.message(self.start()))

    def test_swapped_roles(self):
        self.update(roles={"driver": "agy", "navigator": "claude"})
        self.send(to="driver", sender="navigator")
        text = self.message(self.start())
        self.assertIn("you are the driver", text)
        self.assertIn("tandem watch --as driver --detach", text)
        self.assertIn("1 message(s) pending", text)

    def test_ended_session(self):
        session.end_session(session.load_session(self.dir))
        self.assertEqual(self.start(), {})

    def test_agy_holds_no_role(self):
        self.update(roles={"driver": "claude", "navigator": "someone"})
        self.assertEqual(self.start(), {})
        self.assertIn("agy holds no role", self.hooks_log())

    def test_malformed_stdin(self):
        self.assertEqual(self.out("agy-session-start", "nope"), {})
        self.assertIn("agy-session-start: HookInputError", self.hooks_log())


class PreInvocationTest(AgyHookTestCase):
    def invoke(self, text, **extra):
        data = common("PreInvocation", invocationNum=1, initialNumSteps=0)
        data["lastUserInput"] = text
        data.update(extra)
        return self.out("agy-preinvocation", json.dumps(data))

    def test_human_input_logged_once(self):
        for _ in range(3):
            self.assertEqual(self.invoke("look at retry.go first"), {})
        [rec] = self.consoles()
        self.assertEqual(
            {k: v for k, v in rec.items() if k != "ts"},
            {"kind": "console", "from": "human", "to": "navigator", "peer": "agy", "body": "look at retry.go first"},
        )
        self.assertEqual(self.agy_file("last_input"), "look at retry.go first")

    def test_new_input_logged(self):
        self.invoke("first")
        self.invoke("second")
        self.assertEqual([r["body"] for r in self.consoles()], ["first", "second"])

    def test_multi_line_unicode_verbatim(self):
        body = "  Привет\n\n- check ctx; don't sleep  \n"
        self.invoke(body)
        self.assertEqual(self.consoles()[0]["body"], body)

    def test_doorbell_skipped(self):
        self.invoke(watch.doorbell("navigator"))
        self.invoke("  " + watch.doorbell("navigator"))
        self.assertEqual(self.consoles(), [])

    def test_launcher_prompt_skipped(self):
        self.invoke(hooks.LAUNCHER_PREFIX + " you are the navigator; run tandem recv")
        self.assertEqual(self.consoles(), [])

    def test_same_text_after_a_doorbell_is_logged_again(self):
        self.invoke("status?")
        self.invoke(watch.doorbell("navigator"))
        self.invoke("status?")
        self.assertEqual([r["body"] for r in self.consoles()], ["status?", "status?"])

    def test_blank_and_missing_input(self):
        self.invoke("")
        self.invoke("   \n")
        data = common("PreInvocation")
        del data["lastUserInput"]
        self.assertEqual(self.out("agy-preinvocation", json.dumps(data)), {})
        self.assertEqual(self.consoles(), [])
        self.assertEqual(self.hooks_log(), "")

    def test_non_string_input(self):
        self.assertEqual(self.invoke(["x"]), {})
        self.assertIn("lastUserInput is not a string", self.hooks_log())

    def test_swapped_roles(self):
        self.update(roles={"driver": "agy", "navigator": "claude"})
        self.invoke("go ahead")
        self.assertEqual(self.consoles()[0]["to"], "driver")

    def test_ended_session(self):
        session.end_session(session.load_session(self.dir))
        self.assertEqual(self.invoke("too late"), {})
        self.assertEqual(self.consoles(), [])

    def test_failed_log_is_retried(self):
        with mock.patch.object(session, "write_log_line", side_effect=OSError("disk full")):
            self.assertEqual(self.invoke("retry me"), {})
        self.assertIn("OSError: disk full", self.hooks_log())
        self.assertIsNone(self.agy_file("last_input"))
        self.invoke("retry me")
        self.assertEqual([r["body"] for r in self.consoles()], ["retry me"])

    def test_malformed_stdin(self):
        self.assertEqual(self.out("agy-preinvocation", "{"), {})
        self.assertIn("agy-preinvocation: HookInputError", self.hooks_log())


class StopTest(AgyHookTestCase):
    def setUp(self):
        super().setUp()
        self.clock = 1000.0
        patcher = mock.patch.object(hooks, "_clock", lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stop(self, **kw):
        return self.out("agy-stop", stop(**kw))

    def test_only_on_idle_no_tool_call(self):
        self.send()
        for kw in (
            {"reason": "USER_CANCELED"},
            {"reason": "MAX_INVOCATIONS"},
            {"reason": "ERROR"},
            {"idle": False},
            {"idle": None},
            {"reason": None},
        ):
            self.assertEqual(self.stop(**kw), {}, kw)
        self.assertEqual(watch.read_rung(self.sdir, "navigator"), 0)
        self.assertIsNone(self.agy_file("watchdog"))

    def test_watcher_alive_no_mail(self):
        self.write_watcher()
        self.assertEqual(self.stop(), {})

    def test_watchdog(self):
        out = self.stop()
        self.assertEqual(out["decision"], "continue")
        self.assertIn("running exactly: tandem watch --as navigator --detach", out["reason"])
        self.assertNotIn("recv", out["reason"])

    def test_watchdog_backs_off(self):
        self.assertEqual(self.stop()["decision"], "continue")
        self.clock += hooks.WATCHDOG_BACKOFF - 1
        self.assertEqual(self.stop(), {})
        self.clock += 2
        self.assertEqual(self.stop()["decision"], "continue")

    def test_watchdog_after_session_start_nudge(self):
        self.out("agy-session-start", json.dumps(common("SessionStart")))
        self.assertEqual(self.stop(), {})

    def test_watchdog_clock_skew_or_garbage(self):
        os.makedirs(os.path.join(self.sdir, "peers", "agy"), exist_ok=True)
        for text in ("%f" % (self.clock + 3600), "garbage", ""):
            with open(os.path.join(self.sdir, "peers", "agy", "watchdog"), "w") as f:
                f.write(text)
            self.assertEqual(self.stop()["decision"], "continue", text)

    def test_piggyback_unrung_mail(self):
        self.write_watcher()
        self.send()
        self.send()
        out = self.stop()
        self.assertEqual(out["decision"], "continue")
        self.assertIn("new mail for navigator. Run `tandem recv`", out["reason"])
        self.assertEqual(watch.read_rung(self.sdir, "navigator"), 2)
        # Rung once: the next idle stop leaves the same mail alone.
        self.assertEqual(self.stop(), {})

    def test_already_rung_mail_not_rung_again(self):
        self.write_watcher()
        self.send()
        with session.locked(self.sdir):
            watch.mark_rung(self.sdir, "navigator", 1)
        self.assertEqual(self.stop(), {})
        self.send()
        self.assertEqual(self.stop()["decision"], "continue")
        self.assertEqual(watch.read_rung(self.sdir, "navigator"), 2)

    def test_watchdog_and_mail_together(self):
        self.send()
        out = self.stop()
        self.assertIn("tandem watch --as navigator --detach", out["reason"])
        self.assertIn("Run `tandem recv`", out["reason"])
        self.assertEqual(watch.read_rung(self.sdir, "navigator"), 1)

    def test_other_inbox_ignored(self):
        self.write_watcher()
        self.send(to="driver", sender="navigator")
        self.send(to="human")
        self.assertEqual(self.stop(), {})

    def test_swapped_roles(self):
        self.update(roles={"driver": "agy", "navigator": "claude"})
        self.write_watcher(role="driver")
        self.send(to="driver", sender="navigator")
        self.assertIn("new mail for driver", self.stop()["reason"])

    def test_ended_session(self):
        self.send()
        session.end_session(session.load_session(self.dir))
        self.assertEqual(self.stop(), {})
        self.assertEqual(watch.read_rung(self.sdir, "navigator"), 0)

    def test_removed_session_dir(self):
        shutil.rmtree(self.sdir)
        out, err = self.hook("agy-stop", stop())
        self.assertEqual(out, {})
        self.assertIn("agy-stop", err)

    def test_malformed_stdin(self):
        for stdin in ("", "null", '"x"'):
            self.assertEqual(self.out("agy-stop", stdin), {}, stdin)
        self.assertEqual(len(self.hooks_log().splitlines()), 3)

    def test_unexpected_exception(self):
        with mock.patch.object(watch, "unrung_id", side_effect=RuntimeError("boom")):
            self.assertEqual(self.stop(), {})
        self.assertIn("agy-stop: RuntimeError: boom", self.hooks_log())


class RegistryTest(AgyHookTestCase):
    def test_safe_outputs(self):
        self.assertEqual(hooks.HOOKS["agy-pretool"].safe, ASK)
        for name in ("agy-session-start", "agy-preinvocation", "agy-stop"):
            self.assertEqual((hooks.HOOKS[name].peer, hooks.HOOKS[name].safe), ("agy", {}))

    def test_each_hook_via_shim(self):
        stdins = {
            "agy-session-start": json.dumps(common("SessionStart")),
            "agy-preinvocation": json.dumps(common("PreInvocation", lastUserInput="hello")),
            "agy-stop": stop(idle=False),
        }
        for event, stdin in stdins.items():
            proc = self.run_shim("--session", self.dir, "hook", event, stdin=stdin)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            json.loads(proc.stdout)
        self.assertEqual([r["body"] for r in self.consoles()], ["hello"])
