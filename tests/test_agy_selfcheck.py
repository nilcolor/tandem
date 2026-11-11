import contextlib
import io
import json
import os
import shutil
import sys
from unittest import mock

from tandem import TandemError, cli, launch_agy, session
from tests.helpers import TandemTestCase

# Stands in for agy: answers --version and print-mode /config the way agy 1.5.5 does
# (config under command.data.config), and records each call to FAKE_AGY_LOG.
FAKE_AGY = """#!%s
import json, os, sys, time
args = sys.argv[1:]
rec = {"argv": args, "cwd": os.getcwd()}
if "--settings" in args:
    path = args[args.index("--settings") + 1]
    try:
        with open(path) as f:
            rec["settings"] = json.load(f)
    except Exception:
        rec["settings"] = None
if os.environ.get("FAKE_AGY_LOG"):
    with open(os.environ["FAKE_AGY_LOG"], "a") as f:
        f.write(json.dumps(rec) + "\\n")
mode = os.environ.get("FAKE_AGY_MODE", "ok")
if args == ["--version"]:
    if mode == "noversion":
        sys.exit(1)
    print(os.environ.get("FAKE_AGY_VERSION", "1.5.5"))
    sys.exit(0)
if "-p=/config" in args:
    if mode == "hang":
        time.sleep(30)
    if mode == "error":
        sys.stderr.write("Error: --settings must be a JSON object of settings.json keys\\n")
        sys.exit(1)
    if mode == "badjson":
        print("not json")
        sys.exit(0)
    cfg = dict(rec["settings"], agentMode="", model="fake")
    perms = cfg["permissions"]
    if mode == "drop":
        perms["allow"] = [r for r in perms["allow"] if not r.startswith("unsandboxed(")]
    if mode == "nodeny":
        perms["deny"] = []
    if mode == "nosandbox":
        cfg["enableTerminalSandbox"] = False
    if mode == "extra":
        perms["allow"] = perms["allow"] + ["command(ls)"]
    if mode == "warn":
        sys.stderr.write('unsandboxed is no longer a valid rule, use command() instead\\n')
    print(json.dumps({"status": "SUCCESS", "num_turns": 0, "command": {"name": "config", "data": {"config": cfg}}}))
    sys.exit(0)
with open(os.environ["FAKE_AGY_OUT"], "w") as f:
    json.dump({"argv": args, "cwd": os.getcwd(), "pid": os.getpid(),
               "session": os.environ.get("TANDEM_SESSION"), "role": os.environ.get("TANDEM_ROLE")}, f)
"""


class FakeExec:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, env, cwd):
        self.calls.append((argv, env, cwd))


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
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def config_calls(self):
        return [c for c in self.agy_calls() if "-p=/config" in c["argv"]]

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

    def cache(self):
        with open(launch_agy.check_path(), encoding="utf-8") as f:
            return json.load(f)

    def events(self, name):
        return [r for r in session.read_log(self.sdir) if r.get("kind") == "event" and r.get("event") == name]


class SelfCheckTest(AgyLaunchTestCase):
    def test_check_passes(self):
        _, _, _, err = self.launched("agy")
        self.assertEqual(err, "")
        self.assertEqual(self.profile(), launch_agy.build_profile(self.meta, "navigator"))
        [call] = self.config_calls()
        self.assertEqual(call["argv"][0], "--settings")
        self.assertEqual(call["argv"][2:], ["-p=/config", "--output-format", "json"])
        self.assertEqual(call["settings"], launch_agy.build_profile(self.meta, "navigator"))
        # The check's profile is a throwaway copy, not the session's own file.
        self.assertNotEqual(call["argv"][1], launch_agy.profile_path(self.sdir))
        self.assertFalse(os.path.exists(call["argv"][1]))
        self.assertFalse(os.path.exists(call["cwd"]))
        cache = self.cache()
        self.assertEqual((cache["version"], cache["ok"], cache["reason"]), ("1.5.5", True, ""))
        self.assertTrue(cache["ts"].endswith("Z"))
        self.assertEqual(launch_agy.check_path(), os.path.join(self.tandem_home, "agy-check.json"))

    def test_extra_shown_rules_are_fine(self):
        os.environ["FAKE_AGY_MODE"] = "extra"
        _, _, _, err = self.launched("agy")
        self.assertEqual(err, "")
        self.assertEqual(self.profile()["toolPermission"], "proceed-in-sandbox")

    def assert_fallback(self, mode, reason_part, cached=True):
        os.environ["FAKE_AGY_MODE"] = mode
        _, _, _, err = self.launched("agy")
        self.assertEqual(self.profile(), launch_agy.build_profile(self.meta, "fallback"), mode)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn("self-check failed", err)
        self.assertIn(reason_part, err)
        self.assertIn("fallback profile", err)
        if cached:
            self.assertEqual((self.cache()["ok"], self.cache()["version"]), (False, "1.5.5"))
            self.assertIn(reason_part, self.cache()["reason"])
        else:
            self.assertFalse(os.path.exists(launch_agy.check_path()))
        [event] = self.events("agy_profile")
        self.assertEqual(event["profile"], "fallback")
        self.assertIn(reason_part, event["check"])

    def test_rules_dropped_falls_back(self):
        self.assert_fallback("drop", "permissions.allow lost unsandboxed(go test), unsandboxed(tandem)")

    def test_deny_dropped_falls_back(self):
        self.assert_fallback("nodeny", "permissions.deny lost write_file(*)")

    def test_setting_changed_falls_back(self):
        self.assert_fallback("nosandbox", "enableTerminalSandbox is False")

    def test_parse_error_falls_back(self):
        self.assert_fallback("error", "exited 1: Error: --settings must be a JSON object")

    def test_unsupported_notice_falls_back(self):
        self.assert_fallback("warn", "unsupported")

    def test_no_config_falls_back(self):
        self.assert_fallback("badjson", "printed no config")

    def test_timeout_falls_back_and_is_not_cached(self):
        with mock.patch.object(launch_agy, "CHECK_TIMEOUT", 1):
            self.assert_fallback("hang", "timed out", cached=False)

    def test_no_version_falls_back_and_is_not_cached(self):
        self.assert_fallback("noversion", "agy --version gave no answer", cached=False)
        self.assertEqual(self.config_calls(), [])

    def test_agy_missing(self):
        os.remove(self.agy)
        code, msg, _, calls = self.launch("agy")
        self.assertEqual(code, 1)
        self.assertIn("cannot run agy", msg)
        self.assertEqual(calls, [])

    def test_cache_hit_skips_the_run(self):
        self.launched("agy")
        self.launched("agy")
        self.assertEqual(len(self.config_calls()), 1)
        self.assertEqual(len([c for c in self.agy_calls() if c["argv"] == ["--version"]]), 2)
        self.assertEqual(self.profile()["toolPermission"], "proceed-in-sandbox")

    def test_cached_failure_keeps_fallback_without_rerun(self):
        os.environ["FAKE_AGY_MODE"] = "drop"
        self.launched("agy")
        os.environ["FAKE_AGY_MODE"] = "ok"
        _, _, _, err = self.launched("agy")
        self.assertEqual(len(self.config_calls()), 1)
        self.assertEqual(self.profile()["toolPermission"], "request-review")
        self.assertIn("self-check failed", err)

    def test_version_change_reruns(self):
        os.environ["FAKE_AGY_MODE"] = "drop"
        self.launched("agy")
        os.environ["FAKE_AGY_MODE"] = "ok"
        os.environ["FAKE_AGY_VERSION"] = "1.5.4"
        _, _, _, err = self.launched("agy")
        self.assertEqual(len(self.config_calls()), 2)
        self.assertEqual(err, "")
        self.assertEqual(self.profile()["toolPermission"], "proceed-in-sandbox")
        self.assertEqual((self.cache()["version"], self.cache()["ok"]), ("1.5.4", True))

    def test_corrupt_cache_reruns(self):
        os.makedirs(self.tandem_home, exist_ok=True)
        for text in ("{", json.dumps({"version": "1.5.5"}), json.dumps(["1.5.5"])):
            with open(launch_agy.check_path(), "w") as f:
                f.write(text)
            before = len(self.config_calls())
            self.launched("agy")
            self.assertEqual(len(self.config_calls()), before + 1, text)

    def test_unverified_version_warns_and_keeps_navigator(self):
        os.environ["FAKE_AGY_VERSION"] = "1.6.0"
        for _ in range(2):
            _, _, _, err = self.launched("agy")
            self.assertEqual(self.profile()["toolPermission"], "proceed-in-sandbox")
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertIn("unverified on agy 1.6.0", err)
            self.assertIn("--profile fallback", err)
        self.assertEqual(len(self.config_calls()), 1)

    def test_is_verified(self):
        self.assertTrue(launch_agy.is_verified("1.5.4"))
        self.assertTrue(launch_agy.is_verified("1.5.5"))
        self.assertTrue(launch_agy.is_verified("agy 1.5.5"))
        for version in ("1.5.6", "1.5", "", None, "1.5.5-beta"):
            self.assertFalse(launch_agy.is_verified(version), version)

    def test_check_profile_carries_user_deny(self):
        self.set_user_deny(["command(rm -rf)"])
        self.launched("agy")
        [call] = self.config_calls()
        self.assertEqual(call["settings"]["permissions"]["deny"], ["write_file(*)", "command(rm -rf)"])
        self.assertEqual(self.profile()["permissions"]["deny"], ["write_file(*)", "command(rm -rf)"])

    def test_parse_check_reasons(self):
        profile = launch_agy.build_profile(self.meta, "navigator")
        ok = json.dumps({"command": {"data": {"config": profile}}})
        self.assertIsNone(launch_agy.parse_check(profile, (0, ok, "")))
        self.assertIn("timed out", launch_agy.parse_check(profile, None))
        self.assertIn("exited 2: no output", launch_agy.parse_check(profile, (2, "", "")))
        self.assertIn("printed no config", launch_agy.parse_check(profile, (0, json.dumps({"command": {"data": {"config": []}}}), "")))
        self.assertIn("printed no config", launch_agy.parse_check(profile, (0, json.dumps([1]), "")))
        no_perms = dict(profile)
        del no_perms["permissions"]
        reason = launch_agy.parse_check(profile, (0, json.dumps({"command": {"data": {"config": no_perms}}}), ""))
        self.assertIn("permissions.allow lost", reason)
        warned = 'Invalid "unsandboxed" permission rules found; they are ignored and grant nothing:'
        self.assertIn("unsupported", launch_agy.parse_check(profile, (0, ok, warned)))


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

    def test_driver_argv_and_no_check(self):
        argv, env, _, err = self.launched("agy", "--role", "driver")
        self.assertEqual(argv[5:7], ["--mode", "auto"])
        self.assertTrue(argv[7].startswith("--prompt-interactive=tandem:"))
        self.assertIn("`tandem watch --as driver --detach`", argv[7])
        self.assertIn("intake", argv[7])
        self.assertEqual(len(argv), 8)
        self.assertEqual(env["TANDEM_ROLE"], "driver")
        self.assertEqual(self.agy_calls(), [])
        self.assertEqual(err, "")
        self.assertEqual(self.profile(), launch_agy.build_profile(self.meta, "driver"))
        [event] = self.events("agy_profile")
        self.assertEqual(event["profile"], "driver")
        self.assertNotIn("check", event)

    def test_driver_profile_keeps_user_deny(self):
        self.set_user_deny(["command(rm -rf)"])
        self.launched("agy", "--role", "driver")
        self.assertEqual(self.profile()["permissions"], {"allow": ["command(tandem)", "command(git)"], "deny": ["command(rm -rf)"]})

    def test_profile_fallback_skips_check(self):
        argv, env, _, err = self.launched("agy", "--profile", "fallback")
        self.assertEqual(self.profile(), launch_agy.build_profile(self.meta, "fallback"))
        self.assertEqual(self.agy_calls(), [])
        self.assertEqual(err, "")
        self.assertNotIn("--mode", argv)
        self.assertEqual(env["TANDEM_ROLE"], "navigator")
        [event] = self.events("agy_profile")
        self.assertEqual(event, dict(event, profile="fallback"))
        self.assertNotIn("check", event)

    def test_profile_fallback_for_driver_is_noted(self):
        argv, _, _, err = self.launched("agy", "--role", "driver", "--profile", "fallback")
        self.assertIn("navigator only", err)
        self.assertEqual(self.profile()["toolPermission"], "request-review")
        self.assertIn("--mode", argv)

    def test_profile_auto_explicit(self):
        self.launched("agy", "--profile", "auto")
        self.assertEqual(len(self.config_calls()), 1)

    def test_bad_profile_is_usage_error(self):
        code, _, err = self.run_cli("agy", "--profile", "loose")
        self.assertEqual(code, 1)
        self.assertIn("invalid choice", err)
        with self.assertRaises(TandemError):
            launch_agy.prepare(self.meta, profile="loose")

    def test_event_records_check(self):
        self.launched("agy")
        [event] = self.events("agy_profile")
        self.assertEqual((event["profile"], event["version"], event["check"]), ("navigator", "1.5.5", "ok"))

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
        self.assertEqual(self.agy_calls(), [])
        self.assertFalse(os.path.exists(launch_agy.profile_path(self.sdir)))

    def test_ended_session_refused(self):
        session.end_session(self.meta)
        code, msg, _, calls = self.launch("--session", self.dir, "agy")
        self.assertEqual(code, 1)
        self.assertIn("has ended", msg)
        self.assertEqual(calls, [])
        self.assertEqual(self.agy_calls(), [])


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
        self.assertEqual(len(self.config_calls()), 1)

    def test_check_failure_via_shim(self):
        os.environ["FAKE_AGY_OUT"] = os.path.join(self.tmp, "fake-agy.json")
        os.environ["FAKE_AGY_MODE"] = "error"
        result = self.run_shim("agy", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tandem: agy self-check failed", result.stderr)
        self.assertEqual(self.profile()["toolPermission"], "request-review")

    def test_agy_missing_via_shim(self):
        os.remove(self.agy)
        result = self.run_shim("agy", cwd=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("tandem: cannot run agy", result.stderr)
