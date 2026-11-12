import contextlib
import io
import json
import os
import shutil
import sys
from unittest import mock

from tandem import TandemError, cli, launch_agy, session
from tests.helpers import FakeExec, TandemTestCase

# Stands in for agy: answers --version (FAKE_AGY_VERSION, or FAKE_AGY_MODE "noversion"/"hang"),
# records each call to FAKE_AGY_LOG, and on a launch writes what it was started with to
# FAKE_AGY_OUT.
FAKE_AGY = """#!%s
import json, os, sys, time
args = sys.argv[1:]
if os.environ.get("FAKE_AGY_LOG"):
    with open(os.environ["FAKE_AGY_LOG"], "a") as f:
        f.write(json.dumps({"argv": args}) + "\\n")
mode = os.environ.get("FAKE_AGY_MODE", "ok")
if args == ["--version"]:
    if mode == "noversion":
        sys.exit(1)
    if mode == "hang":
        time.sleep(30)
    print(os.environ.get("FAKE_AGY_VERSION", "1.5.6"))
    sys.exit(0)
with open(os.environ["FAKE_AGY_OUT"], "w") as f:
    json.dump({"argv": args, "cwd": os.getcwd(), "pid": os.getpid(),
               "session": os.environ.get("TANDEM_SESSION"), "role": os.environ.get("TANDEM_ROLE")}, f)
"""


class AgyLaunchTestCase(TandemTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("apix")
        self.meta = session.init_session(cwd=self.repo, name="add-retry", ticket="APIX-7", test_cmds=["go test"])
        self.dir = self.meta["dir"]
        self.sdir = session.session_path(self.dir)
        os.chdir(self.repo)
        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)
        os.symlink(shutil.which("git"), os.path.join(self.bindir, "git"))
        self.agy = os.path.join(self.bindir, "agy")
        with open(self.agy, "w") as f:
            f.write(FAKE_AGY % sys.executable)
        os.chmod(self.agy, 0o755)
        self.agy_log = os.path.join(self.tmp, "agy-calls.jsonl")
        os.environ["PATH"] = self.bindir
        os.environ["FAKE_AGY_LOG"] = self.agy_log
        self.user_settings = os.path.join(self.home, ".gemini", "antigravity-cli", "settings.json")

    def set_user_deny(self, deny):
        os.makedirs(os.path.dirname(self.user_settings), exist_ok=True)
        with open(self.user_settings, "w", encoding="utf-8") as f:
            json.dump({"permissions": {"allow": ["command(ls)"], "deny": deny}}, f)

    def agy_calls(self):
        try:
            with open(self.agy_log, encoding="utf-8") as f:
                return [json.loads(line)["argv"] for line in f]
        except FileNotFoundError:
            return []

    def launch(self, *argv):
        """Run `tandem [argv]` for an agy launch with a fake exec; returns (code, error, stderr, calls)."""
        fake = FakeExec()
        args = cli.build_parser().parse_args(list(argv))
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                code = launch_agy.run_agy(args, exec_fn=fake)
            msg = ""
        except TandemError as exc:
            code, msg = 1, str(exc)
        return code, msg, err.getvalue(), fake.calls

    def launched(self, *argv):
        code, msg, err, calls = self.launch(*argv)
        self.assertEqual((code, msg), (0, ""))
        self.assertEqual(len(calls), 1)
        argv, env, cwd = calls[0]
        return argv, env, cwd, err

    def profile(self):
        with open(launch_agy.profile_path(self.sdir), encoding="utf-8") as f:
            return json.load(f)

    def assert_nothing_written(self):
        self.assertFalse(os.path.exists(launch_agy.profile_path(self.sdir)))
        self.assertFalse(os.path.exists(launch_agy.agents_dir(self.sdir)))
        self.assertEqual(self.events("agy_profile"), [])
        self.assertEqual(self.events("role_claimed"), [])


class VersionTest(AgyLaunchTestCase):
    def test_supported_versions_launch_quietly(self):
        for version in ("1.5.6", "agy 1.5.6", "1.5.10", "1.6.0", "2.0"):
            os.environ["FAKE_AGY_VERSION"] = version
            _, _, _, err = self.launched("agy")
            self.assertEqual(err, "", version)
            self.assertEqual(self.events("agy_profile")[-1]["version"], version)
        self.assertEqual(self.agy_calls(), [["--version"]] * 5)

    def test_older_agy_is_refused_before_anything_is_written(self):
        for version in ("1.5.5", "agy 1.5.4", "1.4.9", "1.5"):
            os.environ["FAKE_AGY_VERSION"] = version
            code, msg, _, calls = self.launch("agy")
            self.assertEqual(code, 1, version)
            self.assertIn("agy %s is too old: tandem needs agy 1.5.6 or later (run `agy update`)" % version, msg)
            self.assertEqual(calls, [])
            self.assert_nothing_written()
        self.assertNotIn("agy", session.load_session(self.dir).get("claims", {}))

    def test_driver_needs_the_same_agy(self):
        os.environ["FAKE_AGY_VERSION"] = "1.5.5"
        code, msg, _, _ = self.launch("agy", "--role", "driver")
        self.assertEqual(code, 1)
        self.assertIn("too old", msg)
        self.assertEqual(session.load_session(self.dir)["roles"]["driver"], "claude")

    def test_unreadable_version_warns_and_launches(self):
        for version in ("1.5.6-beta", "agy dev build", "v1.5.6"):
            os.environ["FAKE_AGY_VERSION"] = version
            _, _, _, err = self.launched("agy")
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertIn("can't tell agy's version (`agy --version` printed %r)" % version, err)
            self.assertIn("tandem needs agy 1.5.6 or later", err)

    def test_no_answer_warns_and_launches(self):
        os.environ["FAKE_AGY_MODE"] = "noversion"
        _, _, _, err = self.launched("agy")
        self.assertIn("can't tell agy's version (no answer from `agy --version`)", err)
        self.assertIsNone(self.events("agy_profile")[-1]["version"])

    def test_hanging_version_times_out_and_launches(self):
        os.environ["FAKE_AGY_MODE"] = "hang"
        with mock.patch.object(launch_agy, "VERSION_TIMEOUT", 1):
            _, _, _, err = self.launched("agy")
        self.assertIn("no answer from `agy --version`", err)

    def test_agy_missing(self):
        os.remove(self.agy)
        code, msg, _, calls = self.launch("agy")
        self.assertEqual(code, 1)
        self.assertIn("cannot run agy", msg)
        self.assertEqual(calls, [])
        self.assert_nothing_written()

    def test_version_number(self):
        cases = {"1.5.6": (1, 5, 6), "agy 1.5.10": (1, 5, 10), "2": (2,), "1.5.6-beta": None, "": None, None: None}
        for text, want in cases.items():
            self.assertEqual(launch_agy.version_number(text), want, text)


class ArgvEnvTest(AgyLaunchTestCase):
    def test_navigator_argv(self):
        argv, env, cwd, _ = self.launched("agy")
        self.assertEqual(argv[:5], ["agy", "--settings", launch_agy.profile_path(self.sdir), "--add-dir", self.sdir])
        self.assertEqual(len(argv), 6)
        self.assertNotIn("--mode", argv)
        prompt = argv[5]
        self.assertTrue(prompt.startswith("--prompt-interactive=tandem:"))
        self.assertIn("navigator", prompt)
        self.assertIn("`tandem watch --as navigator --detach`", prompt)
        self.assertIn("tandem recv", prompt)
        self.assertEqual((env["TANDEM_SESSION"], env["TANDEM_ROLE"]), (self.dir, "navigator"))
        self.assertEqual(cwd, self.repo)
        self.assertEqual(self.profile(), launch_agy.build_profile(self.meta, "navigator"))

    def test_driver_argv(self):
        argv, env, _, err = self.launched("agy", "--role", "driver")
        self.assertEqual(argv[5:7], ["--mode", "accept-edits"])
        self.assertTrue(argv[7].startswith("--prompt-interactive=tandem:"))
        self.assertIn("`tandem watch --as driver --detach`", argv[7])
        self.assertIn("intake", argv[7])
        self.assertEqual(len(argv), 8)
        self.assertEqual(env["TANDEM_ROLE"], "driver")
        self.assertEqual(err, "")
        self.assertEqual(self.profile(), launch_agy.build_profile(session.load_session(self.dir), "driver"))

    def test_profile_event(self):
        self.launched("agy")
        self.launched("agy", "--role", "driver")
        events = self.events("agy_profile")
        self.assertEqual([(e["profile"], e["version"]) for e in events], [("navigator", "1.5.6"), ("driver", "1.5.6")])

    def test_user_deny_is_kept(self):
        self.set_user_deny(["command(rm -rf)"])
        self.launched("agy")
        self.assertEqual(self.profile()["permissions"]["deny"], ["write_file(*)", "command(rm -rf)"])
        self.launched("agy", "--role", "driver")
        self.assertEqual(self.profile()["permissions"]["deny"], ["command(rm -rf)"])

    def test_profile_option_is_gone(self):
        code, _, err = self.run_cli("agy", "--profile", "fallback")
        self.assertEqual(code, 1)
        self.assertIn("unrecognized arguments: --profile", err)

    def test_extra_args_after_prompt(self):
        argv, _, _, _ = self.launched("agy", "--", "--model", "fast")
        self.assertTrue(argv[5].startswith("--prompt-interactive="))
        self.assertEqual(argv[6:], ["--model", "fast"])

    def test_extra_without_separator_is_error(self):
        code, msg, _, calls = self.launch("agy", "stray")
        self.assertEqual(code, 1)
        self.assertIn("after --", msg)
        self.assertEqual(calls, [])

    def test_env_keeps_caller_env_and_cwd_is_toplevel(self):
        os.environ["KEEP_ME"] = "1"
        os.environ["TANDEM_ROLE"] = "driver"
        sub = os.path.join(self.repo, "pkg")
        os.makedirs(sub)
        os.chdir(sub)
        _, env, cwd, _ = self.launched("agy")
        self.assertEqual(env["KEEP_ME"], "1")
        self.assertEqual(env["TANDEM_ROLE"], "navigator")
        self.assertEqual(env["TANDEM_HOME"], self.tandem_home)
        self.assertEqual(cwd, self.repo)

    def test_session_files_written_for_role(self):
        self.launched("agy", "--role", "driver")
        with open(launch_agy.rules_path(self.sdir), encoding="utf-8") as f:
            self.assertIn("## Your role: driver", f.read())
        self.assertTrue(os.path.exists(launch_agy.hooks_path(self.sdir)))

    def test_initial_prompt_is_not_logged_as_console(self):
        argv, _, _, _ = self.launched("agy")
        prompt = argv[5][len("--prompt-interactive="):]
        payload = {"conversationId": "c-1", "hookEventName": "PreInvocation", "lastUserInput": prompt}
        code, out, _ = self.run_cli("--session", self.dir, "hook", "agy-preinvocation", stdin=json.dumps(payload))
        self.assertEqual(code, 0)
        self.assertEqual([r for r in session.read_log(self.sdir) if r.get("kind") == "console"], [])


class RoleClaimTest(AgyLaunchTestCase):
    def test_default_role_and_claim(self):
        self.launched("agy")
        meta = session.load_session(self.dir)
        self.assertEqual(meta["claims"]["agy"]["role"], "navigator")
        self.assertEqual(meta["claims"]["agy"]["pid"], os.getpid())
        [event] = self.events("role_claimed")
        self.assertEqual((event["peer"], event["role"]), ("agy", "navigator"))

    def test_role_flag_swaps(self):
        self.launched("agy", "--role", "driver")
        self.assertEqual(session.load_session(self.dir)["roles"], {"driver": "agy", "navigator": "claude"})

    def test_conflict_with_live_claude(self):
        meta = session.load_session(self.dir)
        meta["claims"] = {"claude": {"role": "driver", "pid": os.getpid(), "ts": "2026-11-11T10:00:00Z"}}
        session.save_session(meta)
        code, msg, _, calls = self.launch("agy", "--role", "driver")
        self.assertEqual(code, 1)
        self.assertIn("claude is already the driver", msg)
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(launch_agy.profile_path(self.sdir)))

    def test_ended_session_refused(self):
        session.end_session(self.meta)
        code, msg, _, calls = self.launch("--session", self.dir, "agy")
        self.assertEqual(code, 1)
        self.assertIn("has ended", msg)
        self.assertEqual(calls, [])


class RealExecTest(AgyLaunchTestCase):
    """The default exec through the shim, with the fake `agy` on PATH."""

    def test_execs_agy_with_same_pid(self):
        out = os.path.join(self.tmp, "fake-agy.json")
        os.environ["FAKE_AGY_OUT"] = out
        result = self.run_shim("agy", "--", "--model", "fast", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        with open(out) as f:
            seen = json.load(f)
        self.assertEqual(seen["cwd"], self.repo)
        self.assertEqual((seen["session"], seen["role"]), (self.dir, "navigator"))
        self.assertEqual(seen["argv"][:4], ["--settings", launch_agy.profile_path(self.sdir), "--add-dir", self.sdir])
        self.assertTrue(seen["argv"][4].startswith("--prompt-interactive=tandem:"))
        self.assertEqual(seen["argv"][5:], ["--model", "fast"])
        self.assertEqual(session.load_session(self.dir)["claims"]["agy"]["pid"], seen["pid"])

    def test_old_agy_via_shim(self):
        os.environ["FAKE_AGY_VERSION"] = "1.5.5"
        result = self.run_shim("agy", cwd=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("tandem: agy 1.5.5 is too old", result.stderr)

    def test_agy_missing_via_shim(self):
        os.remove(self.agy)
        result = self.run_shim("agy", cwd=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("tandem: cannot run agy", result.stderr)
