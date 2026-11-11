import json
import os
import shutil
import subprocess
import sys
from unittest import mock

from tandem import hooks, maildir, session
from tests.helpers import SHIM, TandemTestCase


def payload(prompt, **extra):
    data = {
        "session_id": "abc-123",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    }
    data.update(extra)
    return json.dumps(data)


class ClaudePromptTestCase(TandemTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("apix")
        self.meta = session.init_session(cwd=self.repo, name="fix-csv-bom")
        self.dir = self.meta["dir"]
        self.sdir = session.session_path(self.dir)
        os.chdir(self.repo)

    def hook(self, stdin, *pre):
        return self.run_cli(*(pre or ("--session", self.dir)), "hook", "claude-prompt", stdin=stdin)

    def consoles(self):
        return [r for r in session.read_log(self.sdir) if r.get("kind") == "console"]

    def hooks_log(self):
        try:
            with open(hooks.hooks_log_path(self.sdir, "claude"), encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def set_roles(self, **roles):
        meta = session.load_session(self.dir)
        meta["roles"] = roles
        session.save_session(meta)

    def assert_quiet(self, result):
        code, out, _ = result
        self.assertEqual(code, 0)
        self.assertEqual(out, "")


class NormalPromptTest(ClaudePromptTestCase):
    def test_logs_console_line(self):
        self.assert_quiet(self.hook(payload("use the existing parser")))
        [rec] = self.consoles()
        self.assertEqual(
            {k: v for k, v in rec.items() if k != "ts"},
            {
                "kind": "console",
                "from": "human",
                "to": "driver",
                "peer": "claude",
                "body": "use the existing parser",
            },
        )
        self.assertRegex(rec["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.hooks_log(), "")

    def test_role_is_the_one_claude_holds(self):
        self.set_roles(driver="agy", navigator="claude")
        self.assert_quiet(self.hook(payload("look at the tests first")))
        self.assertEqual(self.consoles()[0]["to"], "navigator")

    def test_role_read_at_hook_time_not_from_env(self):
        os.environ["TANDEM_ROLE"] = "driver"
        self.set_roles(driver="agy", navigator="claude")
        self.hook(payload("x"))
        self.assertEqual(self.consoles()[0]["to"], "navigator")

    def test_body_kept_verbatim(self):
        body = "  first line\n\n  — ünïcode ✓\nlast line  \n"
        self.assert_quiet(self.hook(payload(body)))
        self.assertEqual(self.consoles()[0]["body"], body)

    def test_each_prompt_logged_in_order(self):
        for text in ("one", "two", "one"):
            self.hook(payload(text))
        self.assertEqual([r["body"] for r in self.consoles()], ["one", "two", "one"])

    def test_resolves_from_tandem_session_env(self):
        os.environ["TANDEM_SESSION"] = self.dir
        os.chdir(self.tmp)
        self.assert_quiet(self.run_cli("hook", "claude-prompt", stdin=payload("from env")))
        self.assertEqual(self.consoles()[0]["body"], "from env")

    def test_resolves_from_repo_pointer(self):
        self.assert_quiet(self.run_cli("hook", "claude-prompt", stdin=payload("via pointer")))
        self.assertEqual(self.consoles()[0]["body"], "via pointer")

    def test_extra_fields_ignored(self):
        self.assert_quiet(self.hook(payload("x", permission_mode="default", extra={"a": 1})))
        self.assertEqual(len(self.consoles()), 1)

    def test_interleaves_with_messages(self):
        maildir.deliver(self.meta, maildir.build_envelope("navigator", "driver", "review"))
        self.hook(payload("ok, go"))
        kinds = [r["kind"] for r in session.read_log(self.sdir)]
        self.assertEqual(kinds[-2:], ["message", "console"])

    def test_rendered_by_tandem_log(self):
        self.hook(payload("ship it"))
        code, out, _ = self.run_cli("--session", self.dir, "log")
        self.assertEqual(code, 0)
        self.assertIn("human → driver  console (claude)", out)
        self.assertIn("ship it", out)

    def test_reaches_summary(self):
        self.hook(payload("keep the BOM test"))
        code, out, _ = self.run_cli("--session", self.dir, "done")
        self.assertEqual(code, 0)
        with open(os.path.join(self.sdir, "summary.md"), encoding="utf-8") as f:
            summary = f.read()
        self.assertIn("Human input in consoles (1)", summary)
        self.assertIn("keep the BOM test", summary)


class SkippedPromptTest(ClaudePromptTestCase):
    def test_launcher_prefix_skipped(self):
        self.assert_quiet(self.hook(payload("tandem: you are the driver; start")))
        self.assertEqual(self.consoles(), [])
        self.assertEqual(self.hooks_log(), "")

    def test_bare_prefix_skipped(self):
        self.assert_quiet(self.hook(payload("tandem:")))
        self.assertEqual(self.consoles(), [])

    def test_prefix_only_at_start(self):
        self.hook(payload("about tandem: why is the watcher dead?"))
        self.hook(payload(" tandem: leading space is the user typing"))
        self.hook(payload("tandem status says nothing"))
        self.assertEqual(len(self.consoles()), 3)

    def test_empty_and_blank_prompts_skipped(self):
        for text in ("", "   ", "\n\t\n"):
            self.assert_quiet(self.hook(payload(text)))
        self.assertEqual(self.consoles(), [])
        self.assertEqual(self.hooks_log(), "")


class MalformedInputTest(ClaudePromptTestCase):
    def check_rejected(self, stdin):
        self.assert_quiet(self.hook(stdin))
        self.assertEqual(self.consoles(), [])
        self.assertIn("claude-prompt: HookInputError", self.hooks_log())

    def test_not_json(self):
        self.check_rejected("this is { not json")

    def test_empty_stdin(self):
        self.check_rejected("")

    def test_json_not_an_object(self):
        self.check_rejected(json.dumps(["prompt", "x"]))

    def test_missing_prompt(self):
        self.check_rejected(json.dumps({"session_id": "abc"}))

    def test_prompt_not_a_string(self):
        for value in (None, 42, ["x"], {"text": "x"}):
            with self.subTest(value=value):
                self.check_rejected(payload(value))

    def test_hooks_log_line_is_one_line(self):
        self.hook("line one\nline two")
        lines = self.hooks_log().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^\d{4}-\d\d-\d\dT\S+Z claude-prompt: ")

    def test_failures_append(self):
        self.hook("nope")
        self.hook("[]")
        self.assertEqual(len(self.hooks_log().splitlines()), 2)

    def test_invalid_utf8_via_shim(self):
        raw = b'{"prompt": "caf\xe9 au lait"}'
        proc = subprocess.run(
            [sys.executable, SHIM, "--session", self.dir, "hook", "claude-prompt"],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(os.environ),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        self.assertEqual(self.consoles()[0]["body"], "caf\ufffd au lait")


class EndedOrMissingSessionTest(ClaudePromptTestCase):
    def test_ended_session_logs_nothing(self):
        self.assertEqual(self.run_cli("--session", self.dir, "done")[0], 0)
        before = session.read_log(self.sdir)
        self.assert_quiet(self.hook(payload("too late")))
        self.assertEqual(session.read_log(self.sdir), before)
        self.assertEqual(self.hooks_log(), "")

    def test_ended_via_env(self):
        self.run_cli("--session", self.dir, "done")
        os.environ["TANDEM_SESSION"] = self.dir
        self.assert_quiet(self.run_cli("hook", "claude-prompt", stdin=payload("too late")))
        self.assertEqual(self.consoles(), [])

    def test_unknown_session(self):
        code, out, err = self.hook(payload("x"), "--session", "no-such-session")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("claude-prompt", err)
        self.assertEqual(self.consoles(), [])

    def test_no_session_at_all(self):
        os.chdir(self.tmp)
        self.run_cli("--session", self.dir, "done")
        code, out, err = self.run_cli("hook", "claude-prompt", stdin=payload("x"))
        self.assertEqual((code, out), (0, ""))
        self.assertIn("no active session", err)

    def test_removed_session_dir(self):
        shutil.rmtree(self.sdir)
        result = self.hook(payload("x"))
        self.assert_quiet(result)
        self.assertIn("claude-prompt: TandemError", result[2])

    def test_claude_holds_no_role(self):
        self.set_roles(driver="agy", navigator="someone-else")
        self.assert_quiet(self.hook(payload("x")))
        self.assertEqual(self.consoles(), [])
        self.assertIn("claude holds no role", self.hooks_log())


class FailureIsolationTest(ClaudePromptTestCase):
    def test_write_failure_is_swallowed(self):
        with mock.patch.object(session, "write_log_line", side_effect=OSError("disk full")):
            self.assert_quiet(self.hook(payload("x")))
        self.assertIn("OSError: disk full", self.hooks_log())

    def test_unexpected_exception_is_swallowed(self):
        with mock.patch.object(hooks, "log_console", side_effect=RuntimeError("boom")):
            self.assert_quiet(self.hook(payload("x")))
        self.assertIn("RuntimeError: boom", self.hooks_log())

    def test_read_stdin_failure(self):
        with mock.patch.object(hooks, "read_stdin", side_effect=OSError("stdin closed")):
            code, out, err = self.hook("")
        self.assertEqual((code, out), (0, ""))
        # Raised before the session was resolved, so it goes to stderr.
        self.assertIn("OSError: stdin closed", err)

    def test_hooks_log_unwritable_falls_back_to_stderr(self):
        peers = os.path.join(self.sdir, "peers")
        with open(os.path.join(peers, "claude"), "w") as f:
            f.write("not a dir")
        code, out, err = self.hook("not json")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("HookInputError", err)


class CliTest(ClaudePromptTestCase):
    def test_unknown_event_is_usage_error(self):
        code, out, err = self.run_cli("--session", self.dir, "hook", "no-such-hook", stdin="{}")
        self.assertEqual(code, 1)
        self.assertIn("invalid choice", err)

    def test_event_required(self):
        self.assertEqual(self.run_cli("hook")[0], 1)

    def test_registry(self):
        hook = hooks.HOOKS["claude-prompt"]
        self.assertEqual((hook.name, hook.peer, hook.safe), ("claude-prompt", "claude", None))
        self.assertTrue(all(name == h.name for name, h in hooks.HOOKS.items()))

    def test_shim_invocation_as_generated(self):
        proc = self.run_shim("--session", self.dir, "hook", "claude-prompt", stdin=payload("from shim"))
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertEqual(self.consoles()[0]["body"], "from shim")

    def test_concurrent_prompts_and_sends(self):
        procs = [
            subprocess.Popen(
                [sys.executable, SHIM, "--session", self.dir, "hook", "claude-prompt"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(os.environ),
            )
            for _ in range(6)
        ]
        for i, p in enumerate(procs):
            p.stdin.write(payload("prompt %d" % i).encode("utf-8"))
            p.stdin.close()
        for i in range(3):
            maildir.deliver(self.meta, maildir.build_envelope("navigator", "driver", "msg %d" % i))
        for p in procs:
            self.assertEqual(p.wait(timeout=30), 0)
            self.assertEqual(p.stdout.read(), b"")
            p.stdout.close()
            p.stderr.close()
        bodies = sorted(r["body"] for r in self.consoles())
        self.assertEqual(bodies, sorted("prompt %d" % i for i in range(6)))
