import io
import json
import os
import shutil
import sys

from tandem import TandemError, cli, launch_claude, protocol, session
from tests.helpers import SHIM, FakeExec, TandemTestCase


class LaunchTestCase(TandemTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("apix")
        self.meta = session.init_session(cwd=self.repo, name="fix-csv-bom", ticket="APIX-1", test_cmds=["dip rspec"])
        self.dir = self.meta["dir"]
        self.sdir = session.session_path(self.dir)
        os.chdir(self.repo)

    def launch(self, *argv):
        """Run `tandem [argv] claude …` with a fake exec; returns (code, stderr, exec calls)."""
        fake = FakeExec()
        args = cli.build_parser().parse_args(list(argv))
        try:
            code = launch_claude.run_claude(args, exec_fn=fake)
            err = ""
        except TandemError as exc:
            code, err = 1, str(exc)
        return code, err, fake.calls

    def launched(self, *argv):
        code, err, calls = self.launch(*argv)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(calls), 1)
        return calls[0]

    def settings(self, role="driver"):
        with open(launch_claude.settings_path(self.sdir, role), encoding="utf-8") as f:
            return json.load(f)

    def set_claim(self, peer, role, pid):
        meta = session.load_session(self.dir)
        meta.setdefault("claims", {})[role] = {"peer": peer, "pid": pid, "ts": "2026-11-11T10:00:00Z"}
        session.save_session(meta)

    def assert_no_settings(self):
        for role in session.PAIR_ROLES:
            self.assertFalse(os.path.exists(launch_claude.settings_path(self.sdir, role)), role)


class SettingsTest(LaunchTestCase):
    def test_driver_settings(self):
        self.launched("claude")
        s = self.settings()
        self.assertEqual(
            s["permissions"],
            {
                "allow": [
                    "Bash(tandem send *)",
                    "Bash(tandem recv *)",
                    "Bash(tandem status *)",
                    "Bash(tandem log *)",
                    "Bash(tandem watch *)",
                    "Bash(tandem done *)",
                    "Bash(tandem test dip rspec *)",
                ]
            },
        )
        [stop_group] = s["hooks"]["Stop"]
        [stop] = stop_group["hooks"]
        self.assertEqual(stop["type"], "command")
        self.assertIs(stop["asyncRewake"], True)
        self.assertEqual(stop["timeout"], 14400)
        self.assertEqual(
            stop["command"], "'%s' --session %s watch --as driver --once --deadline 14340" % (SHIM, self.dir)
        )
        [prompt_group] = s["hooks"]["UserPromptSubmit"]
        [prompt] = prompt_group["hooks"]
        self.assertEqual(prompt, {"type": "command", "command": "'%s' --session %s hook claude-prompt" % (SHIM, self.dir)})
        self.assertEqual(set(s), {"hooks", "permissions"})

    def test_navigator_settings_deny_edits(self):
        self.launched("claude", "--role", "navigator")
        s = self.settings("navigator")
        # No `tandem done` (nor init, claude, agy) for a navigator: those prompt.
        self.assertEqual(
            s["permissions"]["allow"],
            [
                "Bash(tandem send *)",
                "Bash(tandem recv *)",
                "Bash(tandem status *)",
                "Bash(tandem log *)",
                "Bash(tandem watch *)",
                "Bash(tandem test dip rspec *)",
            ],
        )
        self.assertEqual(s["permissions"]["deny"], ["Edit", "Write", "NotebookEdit"])
        self.assertIn("--as navigator", s["hooks"]["Stop"][0]["hooks"][0]["command"])

    def test_hook_commands_use_absolute_existing_shim(self):
        self.launched("claude")
        s = self.settings()
        for event in ("Stop", "UserPromptSubmit"):
            command = s["hooks"][event][0]["hooks"][0]["command"]
            self.assertTrue(command.startswith("'/"), command)
            shim = command[1 : command.index("'", 1)]
            self.assertTrue(os.path.isabs(shim))
            self.assertTrue(os.access(shim, os.X_OK), shim)

    def test_shim_path_with_quote_is_escaped(self):
        from tandem import paths

        saved = paths.shim_path
        paths.shim_path = lambda: "/tmp/it's here/bin/tandem"
        try:
            self.assertEqual(paths.shim_command("hook", "x y"), "'/tmp/it'\\''s here/bin/tandem' hook 'x y'")
        finally:
            paths.shim_path = saved

    def test_protocol_file_matches_render(self):
        self.launched("claude", "--role", "navigator")
        meta = session.load_session(self.dir)
        with open(launch_claude.protocol_path(self.sdir, "navigator"), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text, protocol.render(meta, "claude"))
        self.assertIn("## Your role: navigator", text)
        self.assertIn("## Your harness: Claude Code", text)

    def test_guides_written(self):
        self.launched("claude", "--role", "navigator")
        guides = protocol.session_guides_dir(self.sdir)
        self.assertEqual(sorted(os.listdir(guides)), sorted(protocol.guide_sources()))

    def test_relaunch_rewrites_config_for_new_role(self):
        self.launched("claude")
        self.launched("claude", "--role", "navigator")
        self.assertIn("deny", self.settings("navigator")["permissions"])
        self.launched("claude", "--role", "driver")
        self.assertNotIn("deny", self.settings("driver")["permissions"])


class ArgvEnvTest(LaunchTestCase):
    def test_argv(self):
        argv, _, _ = self.launched("claude")
        with open(launch_claude.protocol_path(self.sdir, "driver"), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(
            argv[:7],
            ["claude", "--add-dir", self.sdir, "--settings", launch_claude.settings_path(self.sdir, "driver"),
             "--append-system-prompt", text],
        )
        self.assertEqual(argv[7], "--")
        self.assertEqual(len(argv), 9)
        self.assertTrue(argv[8].startswith("tandem:"))
        self.assertIn("driver", argv[8])
        self.assertIn("tandem recv", argv[8])

    def test_navigator_prompt(self):
        argv, _, _ = self.launched("claude", "--role", "navigator")
        self.assertTrue(argv[-1].startswith("tandem:"))
        self.assertIn("navigator", argv[-1])

    def test_extra_args_pass_through_before_prompt(self):
        argv, _, _ = self.launched("claude", "--", "--model", "opus", "--allowed-tools", "Read")
        self.assertEqual(argv[7:11], ["--model", "opus", "--allowed-tools", "Read"])
        self.assertEqual(argv[11], "--")
        self.assertTrue(argv[12].startswith("tandem:"))

    def test_extra_without_separator_is_error(self):
        code, err, calls = self.launch("claude", "stray")
        self.assertEqual(code, 1)
        self.assertIn("after --", err)
        self.assertEqual(calls, [])

    def test_env_and_cwd(self):
        os.environ["KEEP_ME"] = "1"
        os.environ["TANDEM_ROLE"] = "navigator"
        _, env, cwd = self.launched("--session", self.dir, "claude")
        self.assertEqual(env["TANDEM_SESSION"], self.dir)
        self.assertEqual(env["TANDEM_ROLE"], "driver")
        self.assertEqual(env["KEEP_ME"], "1")
        self.assertEqual(env["TANDEM_HOME"], self.tandem_home)
        self.assertEqual(cwd, self.repo)

    def test_cwd_is_toplevel_from_subdir(self):
        sub = os.path.join(self.repo, "lib", "deep")
        os.makedirs(sub)
        os.chdir(sub)
        _, _, cwd = self.launched("claude")
        self.assertEqual(cwd, self.repo)

    def test_initial_prompt_is_not_logged_as_console(self):
        argv, _, _ = self.launched("claude")
        code, out, _ = self.run_cli(
            "--session", self.dir, "hook", "claude-prompt", stdin=json.dumps({"prompt": argv[-1]})
        )
        self.assertEqual((code, out), (0, ""))
        self.assertEqual([r for r in session.read_log(self.sdir) if r.get("kind") == "console"], [])


class RoleClaimTest(LaunchTestCase):
    def test_default_role_from_session(self):
        self.launched("claude")
        meta = session.load_session(self.dir)
        self.assertEqual(meta["roles"], {"driver": "claude", "navigator": "agy"})
        self.assertEqual(meta["claims"]["driver"]["peer"], "claude")
        self.assertEqual(meta["claims"]["driver"]["pid"], os.getpid())
        [event] = self.events("role_claimed")
        self.assertEqual((event["peer"], event["role"]), ("claude", "driver"))
        self.assertNotIn("was", event)

    def test_role_flag_swaps_roles(self):
        _, env, _ = self.launched("claude", "--role", "navigator")
        meta = session.load_session(self.dir)
        self.assertEqual(meta["roles"], {"driver": "agy", "navigator": "claude"})
        self.assertEqual(env["TANDEM_ROLE"], "navigator")
        [event] = self.events("role_claimed")
        self.assertEqual((event["role"], event["was"]), ("navigator", "driver"))

    def test_role_from_session_after_earlier_swap(self):
        meta = session.load_session(self.dir)
        meta["roles"] = {"driver": "agy", "navigator": "claude"}
        session.save_session(meta)
        _, env, _ = self.launched("claude")
        self.assertEqual(env["TANDEM_ROLE"], "navigator")

    def test_conflict_with_live_peer(self):
        self.set_claim("agy", "navigator", os.getppid())
        before = session.load_session(self.dir)
        code, err, calls = self.launch("claude", "--role", "navigator")
        self.assertEqual(code, 1)
        self.assertIn("agy is already the navigator", err)
        self.assertEqual(calls, [])
        self.assertEqual(session.load_session(self.dir), before)
        self.assertEqual(self.events("role_claimed"), [])
        self.assert_no_settings()

    def test_cli_reports_conflict(self):
        self.set_claim("agy", "navigator", os.getppid())
        code, out, err = self.run_cli("claude", "--role", "navigator")
        self.assertEqual(code, 1)
        self.assertIn("tandem: agy is already the navigator", err)

    def test_dead_peer_claim_is_released(self):
        self.set_claim("agy", "navigator", self.dead_pid())
        self.launched("claude", "--role", "navigator")
        self.assertEqual(session.load_session(self.dir)["roles"], {"driver": "agy", "navigator": "claude"})

    def test_live_peer_in_other_role_is_no_conflict(self):
        self.set_claim("agy", "navigator", os.getppid())
        _, env, _ = self.launched("claude")
        self.assertEqual(env["TANDEM_ROLE"], "driver")

    def test_peer_holding_no_role_needs_flag(self):
        meta = session.load_session(self.dir)
        meta["roles"] = {"driver": "copilot", "navigator": "agy"}
        session.save_session(meta)
        code, err, calls = self.launch("claude")
        self.assertEqual(code, 1)
        self.assertIn("--role", err)
        self.launched("claude", "--role", "driver")
        self.assertEqual(session.load_session(self.dir)["roles"], {"driver": "claude", "navigator": "agy"})

    def test_claim_shared_by_other_launchers(self):
        meta, role = session.claim_role(self.dir, "agy", "driver", pid=4242)
        self.assertEqual(role, "driver")
        self.assertEqual(meta["roles"], {"driver": "agy", "navigator": "claude"})
        self.assertEqual(meta["claims"], {"driver": {"peer": "agy", "pid": 4242, "ts": meta["claims"]["driver"]["ts"]}})

    def test_bad_role(self):
        with self.assertRaises(TandemError):
            session.claim_role(self.dir, "claude", "human")

    def test_session_removed_before_the_claim(self):
        os.remove(os.path.join(self.sdir, "session.json"))
        with self.assertRaises(TandemError) as ctx:
            session.claim_role(self.dir, "claude", "driver")
        self.assertIn("no longer exists", str(ctx.exception))

    def test_bad_role_flag_is_usage_error(self):
        code, _, err = self.run_cli("claude", "--role", "human")
        self.assertEqual(code, 1)
        self.assertIn("invalid choice", err)


class SameHarnessPairTest(LaunchTestCase):
    """Two harnesses of one kind: claims are keyed by role, and a running harness keeps its role."""

    def set_roles(self, **roles):
        meta = session.load_session(self.dir)
        meta["roles"] = roles
        session.save_session(meta)

    def test_second_claude_joins_as_the_other_role(self):
        self.set_claim("claude", "driver", os.getppid())
        err = io.StringIO()
        meta, role = session.claim_for_launch(self.dir, "claude", "navigator", err=err)
        self.assertEqual(role, "navigator")
        self.assertEqual(meta["roles"], {"driver": "claude", "navigator": "claude"})
        self.assertEqual(
            {r: (c["peer"], c["pid"]) for r, c in meta["claims"].items()},
            {"driver": ("claude", os.getppid()), "navigator": ("claude", os.getpid())},
        )
        self.assertIn("claude is the navigator and the driver", err.getvalue())
        self.assertNotIn("launch the driver", err.getvalue())
        [event] = self.events("role_claimed")
        self.assertNotIn("was", event)

    def test_relaunch_alone_names_the_other_launch(self):
        self.set_roles(driver="claude", navigator="claude")
        err = io.StringIO()
        session.claim_for_launch(self.dir, "claude", "navigator", err=err)
        self.assertIn("launch the driver with `tandem claude --role driver`", err.getvalue())

    def test_each_claude_gets_its_own_config(self):
        self.set_claim("claude", "driver", os.getppid())
        argv, env, _ = self.launched("claude", "--role", "navigator")
        self.assertEqual(env["TANDEM_ROLE"], "navigator")
        self.assertEqual(argv[argv.index("--settings") + 1], launch_claude.settings_path(self.sdir, "navigator"))
        self.assertIn("deny", self.settings("navigator")["permissions"])
        self.assertFalse(os.path.exists(launch_claude.settings_path(self.sdir, "driver")))

    def test_second_launch_without_role_names_the_flag(self):
        self.set_claim("claude", "driver", os.getppid())
        code, err, calls = self.launch("claude")
        self.assertEqual(code, 1)
        self.assertIn("claude is already the driver", err)
        self.assertIn("pass --role navigator to run a second claude as the navigator", err)
        self.assertEqual(calls, [])

    def test_running_role_is_refused_to_the_same_harness(self):
        self.set_claim("claude", "driver", os.getppid())
        code, err, calls = self.launch("claude", "--role", "driver")
        self.assertEqual(code, 1)
        self.assertIn("claude is already the driver", err)
        self.assertEqual(calls, [])

    def test_pair_default_is_the_role_nobody_runs(self):
        self.set_roles(driver="claude", navigator="claude")
        self.set_claim("claude", "navigator", os.getppid())
        _, env, _ = self.launched("claude")
        self.assertEqual(env["TANDEM_ROLE"], "driver")

    def test_pair_with_neither_running_needs_the_flag(self):
        self.set_roles(driver="claude", navigator="claude")
        code, err, _ = self.launch("claude")
        self.assertEqual(code, 1)
        self.assertIn("claude holds both roles", err)
        _, env, _ = self.launched("claude", "--role", "navigator")
        self.assertEqual(env["TANDEM_ROLE"], "navigator")
        self.assertEqual(session.load_session(self.dir)["roles"], {"driver": "claude", "navigator": "claude"})

    def test_pair_with_both_running_is_refused(self):
        self.set_roles(driver="claude", navigator="claude")
        self.set_claim("claude", "driver", os.getppid())
        self.set_claim("claude", "navigator", os.getppid())
        code, err, _ = self.launch("claude")
        self.assertEqual(code, 1)
        self.assertIn("both are running", err)

    def test_other_harness_takes_a_pair_role(self):
        self.set_roles(driver="claude", navigator="claude")
        meta, role = session.claim_role(self.dir, "agy", "navigator", pid=4242)
        self.assertEqual(meta["roles"], {"driver": "claude", "navigator": "agy"})

    def test_quit_harness_still_swaps(self):
        self.set_claim("claude", "driver", self.dead_pid())
        self.launched("claude", "--role", "navigator")
        meta = session.load_session(self.dir)
        self.assertEqual(meta["roles"], {"driver": "agy", "navigator": "claude"})
        # The old driver claim went with the swap.
        self.assertEqual(list(meta["claims"]), ["navigator"])

    def test_peer_keyed_claims_from_older_sessions(self):
        meta = session.load_session(self.dir)
        meta["claims"] = {"agy": {"role": "navigator", "pid": os.getppid(), "ts": "2026-11-11T10:00:00Z"}}
        session.save_session(meta)
        code, err, _ = self.launch("claude", "--role", "navigator")
        self.assertEqual(code, 1)
        self.assertIn("agy is already the navigator", err)
        self.launched("claude")
        claims = session.load_session(self.dir)["claims"]
        self.assertEqual(sorted(claims), ["driver", "navigator"])
        self.assertEqual(claims["navigator"]["peer"], "agy")


class EndedSessionTest(LaunchTestCase):
    def test_ended_session_refused(self):
        session.end_session(self.meta)
        code, err, calls = self.launch("--session", self.dir, "claude")
        self.assertEqual(code, 1)
        self.assertIn("has ended", err)
        self.assertEqual(calls, [])
        self.assert_no_settings()
        self.assertEqual(self.events("role_claimed"), [])

    def test_ended_session_via_env(self):
        session.end_session(self.meta)
        os.environ["TANDEM_SESSION"] = self.dir
        code, err, calls = self.launch("claude")
        self.assertEqual(code, 1)
        self.assertIn("has ended", err)

    def test_no_session(self):
        session.end_session(self.meta)
        code, err, calls = self.launch("claude")
        self.assertEqual(code, 1)
        self.assertIn("no active session", err)
        self.assertEqual(calls, [])


FAKE_CLAUDE = """#!%s
import json, os, sys
with open(os.environ["FAKE_CLAUDE_OUT"], "w") as f:
    json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "pid": os.getpid(),
               "session": os.environ.get("TANDEM_SESSION"), "role": os.environ.get("TANDEM_ROLE")}, f)
"""


class RealExecTest(LaunchTestCase):
    """The default exec through the shim, with a fake `claude` on PATH."""

    def bin_dir(self, with_claude):
        bindir = os.path.join(self.tmp, "bin-%s" % with_claude)
        os.makedirs(bindir)
        os.symlink(shutil.which("git"), os.path.join(bindir, "git"))
        if with_claude:
            path = os.path.join(bindir, "claude")
            with open(path, "w") as f:
                f.write(FAKE_CLAUDE % sys.executable)
            os.chmod(path, 0o755)
        return bindir

    def test_execs_claude_with_same_pid(self):
        out = os.path.join(self.tmp, "fake-claude.json")
        os.environ["PATH"] = self.bin_dir(True)
        os.environ["FAKE_CLAUDE_OUT"] = out
        result = self.run_shim("claude", "--", "--model", "opus", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(out) as f:
            seen = json.load(f)
        self.assertEqual(seen["cwd"], self.repo)
        self.assertEqual((seen["session"], seen["role"]), (self.dir, "driver"))
        self.assertEqual(seen["argv"][:2], ["--add-dir", self.sdir])
        self.assertEqual(seen["argv"][6:8], ["--model", "opus"])
        self.assertTrue(seen["argv"][-1].startswith("tandem:"))
        # exec keeps the launcher's pid, so the claim tracks the running harness.
        self.assertEqual(session.load_session(self.dir)["claims"]["driver"]["pid"], seen["pid"])

    def test_repo_gone(self):
        argv, env, _ = launch_claude.prepare(self.meta)
        cwd = os.getcwd()
        with self.assertRaises(TandemError) as caught:
            session.exec_harness(argv, env, os.path.join(self.tmp, "no-such-repo"))
        self.assertIn("cannot enter", str(caught.exception))
        self.assertEqual(os.getcwd(), cwd)

    def test_claude_missing(self):
        os.environ["PATH"] = self.bin_dir(False)
        result = self.run_shim("claude", cwd=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("tandem: cannot run claude", result.stderr)
