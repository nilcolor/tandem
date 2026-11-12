import json
import os
import subprocess
from unittest import mock

from tandem import TandemError, hooks, launch_agy, protocol, session
from tests.helpers import SHIM, TandemTestCase

EVENTS = {"PreToolUse", "SessionStart", "PreInvocation", "Stop"}
HOOK_EVENTS = (
    ("PreToolUse", "agy-pretool"),
    ("SessionStart", "agy-session-start"),
    ("PreInvocation", "agy-preinvocation"),
    ("Stop", "agy-stop"),
)
ROLES = ("navigator", "driver")
ASK = {"decision": "ask"}
NAV_TANDEM = ["tandem send", "tandem recv", "tandem status", "tandem log", "tandem watch"]
DRIVER_TANDEM = NAV_TANDEM + ["tandem done"]


def rules(kind, prefixes):
    return ["%s(%s)" % (kind, p) for p in prefixes]


def read_git(repo):
    subs = ["status", "diff", "log", "show"]
    return ["git " + s for s in subs] + ["git -C %s %s" % (repo, s) for s in subs]


def pretool_payload(command):
    return json.dumps(
        {
            "conversationId": "c-1",
            "hookEventName": "PreToolUse",
            "cwd": "/tmp/repo",
            "lastUserInput": "",
            "toolCall": {"id": "t1", "name": "run_command", "args": {"CommandLine": command}},
        }
    )


class AgyFilesTestCase(TandemTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("apix")
        with open(os.path.join(self.repo, "go.mod"), "w") as f:
            f.write("module apix\n")
        self.meta = session.init_session(cwd=self.repo, name="add-retry", ticket="APIX-7")
        self.dir = self.meta["dir"]
        self.sdir = session.session_path(self.dir)
        os.chdir(self.repo)

    def new_session(self, name, test_cmds):
        return session.init_session(cwd=self.repo, name=name, test_cmds=test_cmds, replace=True)

    def read_json(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def read_text(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def handler_command(self, event):
        spec = self.read_json(launch_agy.hooks_path(self.sdir))["tandem"][event]
        if event == "PreToolUse":
            return spec[0]["hooks"][0]["command"]
        return spec[0]["command"]

    def run_handler(self, event, stdin):
        """Run a generated handler the way agy does: `sh -c` from the dir holding hooks.json."""
        return subprocess.run(
            ["sh", "-c", self.handler_command(event)],
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            cwd=launch_agy.agents_dir(self.sdir),
            env=dict(os.environ),
        )


class HooksJsonTest(AgyFilesTestCase):
    def setUp(self):
        super().setUp()
        launch_agy.write_agents_dir(self.meta, "navigator")
        self.doc = self.read_json(launch_agy.hooks_path(self.sdir))

    def command(self, hook):
        return "'%s' --session %s hook %s" % (SHIM, self.dir, hook)

    def test_one_hook_name_at_top_level(self):
        self.assertEqual(list(self.doc), ["tandem"])
        self.assertFalse(set(self.doc) & EVENTS)
        self.assertNotIn("hooks", self.doc)

    def test_events_under_the_hook_name(self):
        self.assertEqual(set(self.doc["tandem"]), EVENTS)

    def test_pretool_is_a_run_command_group(self):
        self.assertEqual(
            self.doc["tandem"]["PreToolUse"],
            [{"matcher": "run_command", "hooks": [{"type": "command", "command": self.command("agy-pretool")}]}],
        )

    def test_other_events_are_flat_handler_lists(self):
        for event, hook in (("SessionStart", "agy-session-start"), ("PreInvocation", "agy-preinvocation"), ("Stop", "agy-stop")):
            self.assertEqual(self.doc["tandem"][event], [{"type": "command", "command": self.command(hook)}], event)

    def test_every_handler_names_a_registered_agy_hook(self):
        for event, hook in HOOK_EVENTS:
            self.assertIn(hook, hooks.HOOKS)
            self.assertEqual(hooks.HOOKS[hook].peer, "agy")
            self.assertTrue(self.handler_command(event).endswith(" hook " + hook))

    def test_shim_is_absolute_and_single_quoted(self):
        cmd = self.handler_command("Stop")
        self.assertTrue(os.path.isabs(SHIM))
        self.assertTrue(cmd.startswith("'%s' " % SHIM))

    def test_same_for_both_roles(self):
        launch_agy.write_agents_dir(self.meta, "driver")
        self.assertEqual(self.read_json(launch_agy.hooks_path(self.sdir)), self.doc)

    def test_matches_build_hooks(self):
        self.assertEqual(self.doc, launch_agy.build_hooks(self.meta))

    def test_pretool_handler_runs_under_sh(self):
        proc = self.run_handler("PreToolUse", pretool_payload("go test ./..."))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), ASK)
        proc = self.run_handler("PreToolUse", pretool_payload("touch x"))
        self.assertEqual(json.loads(proc.stdout)["decision"], "force_ask")

    def test_other_handlers_run_under_sh(self):
        payloads = {
            "SessionStart": {"conversationId": "c-1", "hookEventName": "SessionStart"},
            "PreInvocation": {"conversationId": "c-1", "hookEventName": "PreInvocation", "lastUserInput": "hello"},
            "Stop": {"conversationId": "c-1", "hookEventName": "Stop", "terminationReason": "ERROR"},
        }
        for event, payload in payloads.items():
            proc = self.run_handler(event, json.dumps(payload))
            self.assertEqual(proc.returncode, 0, (event, proc.stderr))
            self.assertIsInstance(json.loads(proc.stdout), dict, event)
        self.assertEqual(self.read_text(os.path.join(self.sdir, "peers", "agy", "conversation_id")).strip(), "c-1")
        consoles = [r for r in session.read_log(self.sdir) if r.get("kind") == "console"]
        self.assertEqual([(c["peer"], c["to"], c["body"]) for c in consoles], [("agy", "navigator", "hello")])


class RulesTest(AgyFilesTestCase):
    def test_rules_are_the_rendered_protocol(self):
        for role in ("navigator", "driver"):
            launch_agy.write_agents_dir(self.meta, role)
            self.assertEqual(self.read_text(launch_agy.rules_path(self.sdir)), protocol.render(self.meta, "agy", role))

    def test_navigator_rules_carry_the_watcher_command(self):
        launch_agy.write_agents_dir(self.meta, "navigator")
        text = self.read_text(launch_agy.rules_path(self.sdir))
        self.assertIn("tandem watch --as navigator --detach", text)
        self.assertIn("APIX-7", text)

    def test_rules_path(self):
        self.assertEqual(launch_agy.rules_path(self.sdir), os.path.join(self.sdir, ".agents", "rules", "AGENTS.md"))
        self.assertEqual(launch_agy.write_agents_dir(self.meta, "navigator"), os.path.join(self.sdir, ".agents"))

    def test_huge_brief_stays_under_cap(self):
        meta = session.init_session(cwd=self.repo, name="huge", task="x" * 100000, test_cmds=["go test"] * 50, replace=True)
        launch_agy.write_agents_dir(meta, "navigator")
        size = os.path.getsize(launch_agy.rules_path(session.session_path(meta["dir"])))
        self.assertLess(size, launch_agy.RULES_CAP)

    def test_multibyte_brief_stays_under_cap(self):
        # The brief is cut by bytes: 6000 CJK characters or emoji are 18-24 KB on their own.
        for char in ("日", "😀"):
            meta = session.init_session(cwd=self.repo, name="mb", task=char * 6000, replace=True)
            launch_agy.write_agents_dir(meta, "navigator")
            size = os.path.getsize(launch_agy.rules_path(session.session_path(meta["dir"])))
            self.assertLess(size, launch_agy.RULES_CAP, char)

    def test_over_cap_writes_nothing(self):
        with mock.patch.object(protocol, "render", return_value="y" * (launch_agy.RULES_CAP + 1)):
            with self.assertRaises(TandemError) as ctx:
                launch_agy.write_agents_dir(self.meta, "navigator")
        self.assertIn("cap", str(ctx.exception))
        self.assertFalse(os.path.exists(launch_agy.hooks_path(self.sdir)))
        self.assertFalse(os.path.exists(launch_agy.rules_path(self.sdir)))

    def test_unicode_counts_bytes(self):
        text = "é" * (launch_agy.RULES_CAP // 2 + 1)
        with mock.patch.object(protocol, "render", return_value=text):
            with self.assertRaises(TandemError):
                launch_agy.render_rules(self.meta, "navigator")

    def test_rewrite_replaces_old_role(self):
        launch_agy.write_agents_dir(self.meta, "driver")
        launch_agy.write_agents_dir(self.meta, "navigator")
        self.assertEqual(self.read_text(launch_agy.rules_path(self.sdir)), protocol.render(self.meta, "agy", "navigator"))
        leftovers = [n for n in os.listdir(os.path.dirname(launch_agy.rules_path(self.sdir))) if n != "AGENTS.md"]
        self.assertEqual(leftovers, [])


class ProfileTest(AgyFilesTestCase):
    def test_navigator(self):
        self.assertEqual(
            launch_agy.build_profile(self.meta, "navigator"),
            {
                "toolPermission": "request-review",
                "enableTerminalSandbox": False,
                "permissions": {
                    "allow": rules("command", NAV_TANDEM + ["go test"] + read_git(self.meta["repo"])),
                    "deny": ["write_file(*)"],
                },
            },
        )

    def test_read_only_git_skips_dash_c_for_paren_paths(self):
        self.assertEqual(launch_agy.read_only_git("/w/a(b)"), ["git status", "git diff", "git log", "git show"])
        self.assertIn("git -C /w/ab diff", launch_agy.read_only_git("/w/ab"))

    def test_driver(self):
        self.assertEqual(
            launch_agy.build_profile(self.meta, "driver"),
            {
                "toolPermission": "request-review",
                "enableTerminalSandbox": False,
                "permissions": {
                    "allow": rules("command", DRIVER_TANDEM + ["go test", "git"])
                    + rules("write_file", [os.path.join(self.sdir, n) for n in ("plan.md", "decisions.md")]),
                    "deny": [],
                },
            },
        )

    def test_session_file_writes_skip_paren_paths(self):
        meta = dict(self.meta, dir="odd(dir)")
        self.assertEqual(launch_agy.session_file_writes(meta), [])

    def test_no_bare_tandem_rule(self):
        # A bare `tandem` rule would pre-approve `tandem claude`, `init --replace` and `done`.
        for role in ROLES:
            allow = launch_agy.build_profile(self.meta, role)["permissions"]["allow"]
            self.assertFalse([r for r in allow if r.endswith("(tandem)")], role)

    def test_every_profile_writes_allow_and_deny(self):
        for role in ROLES:
            path = launch_agy.write_profile(self.meta, role)
            perms = self.read_json(path)["permissions"]
            self.assertEqual(set(perms), {"allow", "deny"}, role)
            self.assertIsInstance(perms["allow"], list)
            self.assertIsInstance(perms["deny"], list)

    def test_default_path(self):
        path = launch_agy.write_profile(self.meta, "navigator")
        self.assertEqual(path, os.path.join(self.sdir, "peers", "agy", "profile.json"))
        self.assertEqual(self.read_json(path), launch_agy.build_profile(self.meta, "navigator"))

    def test_write_session_files(self):
        launch_agy.write_session_files(self.meta, "navigator")
        self.assertEqual(self.read_json(launch_agy.profile_path(self.sdir)), launch_agy.build_profile(self.meta, "navigator"))
        self.assertTrue(os.path.exists(launch_agy.hooks_path(self.sdir)))
        self.assertTrue(os.path.exists(launch_agy.rules_path(self.sdir)))
        launch_agy.write_session_files(self.meta, "driver")
        self.assertEqual(self.read_json(launch_agy.profile_path(self.sdir))["permissions"]["deny"], [])
        self.assertEqual(self.read_text(launch_agy.rules_path(self.sdir)), protocol.render(self.meta, "agy", "driver"))


class UserDenyTest(AgyFilesTestCase):
    """A written deny replaces agy's global one, so the user's own denies are carried over."""

    def write_settings(self, doc):
        path = os.path.join(self.home, ".gemini", "antigravity-cli", "settings.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc if isinstance(doc, str) else json.dumps(doc))
        return path

    def deny(self, kind):
        return launch_agy.build_profile(self.meta, kind)["permissions"]["deny"]

    def test_read_from_home(self):
        self.write_settings({"permissions": {"allow": ["command(ls)"], "deny": ["command(rm -rf)"]}})
        self.assertEqual(launch_agy.user_deny(), ["command(rm -rf)"])
        self.assertEqual(self.deny("navigator"), ["write_file(*)", "command(rm -rf)"])
        self.assertEqual(self.deny("driver"), ["command(rm -rf)"])
        launch_agy.write_session_files(self.meta, "navigator")
        self.assertEqual(self.read_json(launch_agy.profile_path(self.sdir))["permissions"]["deny"], ["write_file(*)", "command(rm -rf)"])

    def test_user_allow_is_not_carried_over(self):
        self.write_settings({"permissions": {"allow": ["command(ls)"], "deny": []}})
        for role in ROLES:
            self.assertNotIn("command(ls)", launch_agy.build_profile(self.meta, role)["permissions"]["allow"], role)

    def test_deduplicated(self):
        self.write_settings({"permissions": {"deny": ["write_file(*)", "command(rm -rf)", "command(rm -rf)"]}})
        self.assertEqual(self.deny("navigator"), ["write_file(*)", "command(rm -rf)"])
        self.assertEqual(self.deny("driver"), ["write_file(*)", "command(rm -rf)"])

    def test_missing_file(self):
        self.assertEqual(launch_agy.user_deny(), [])
        self.assertEqual(self.deny("navigator"), ["write_file(*)"])

    def test_unreadable_file(self):
        path = self.write_settings({"permissions": {"deny": ["command(rm -rf)"]}})
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o600)
        if os.access(path, os.R_OK):
            self.skipTest("this user can read a mode-0 file")
        self.assertEqual(launch_agy.user_deny(), [])

    def test_directory_in_place_of_the_file(self):
        os.makedirs(os.path.join(self.home, ".gemini", "antigravity-cli", "settings.json"))
        self.assertEqual(launch_agy.user_deny(), [])

    def test_malformed_shapes_give_nothing(self):
        for doc in ("{", "[]", "null", {"permissions": []}, {"permissions": {"deny": "command(rm)"}}, {"other": 1}):
            self.write_settings(doc)
            self.assertEqual(launch_agy.user_deny(), [], doc)

    def test_non_string_and_blank_rules_skipped(self):
        self.write_settings({"permissions": {"deny": [1, None, "", "  ", {"x": 1}, "command(rm -rf)"]}})
        self.assertEqual(launch_agy.user_deny(), ["command(rm -rf)"])

    def test_user_file_is_never_written(self):
        path = self.write_settings({"permissions": {"deny": ["command(rm -rf)"]}})
        before = (self.read_text(path), os.stat(path).st_mtime_ns)
        for role in ROLES:
            launch_agy.write_profile(self.meta, role)
        self.assertEqual((self.read_text(path), os.stat(path).st_mtime_ns), before)


class TestPrefixFlowTest(AgyFilesTestCase):
    def setUp(self):
        super().setUp()
        self.meta = self.new_session("specs", ["dip rspec", "bundle exec rspec"])

    def allow(self, meta, role="navigator"):
        return launch_agy.build_profile(meta, role)["permissions"]["allow"]

    def test_exact_commands_never_the_bare_tool(self):
        for role in ROLES:
            allow = self.allow(self.meta, role)
            self.assertIn("command(dip rspec)", allow, role)
            self.assertIn("command(bundle exec rspec)", allow, role)
            self.assertNotIn("command(dip)", allow, role)
            self.assertNotIn("command(bundle)", allow, role)

    def test_detected_prefix(self):
        self.assertIn("command(go test)", self.allow(self.new_session("detected", None)))

    def test_no_test_commands(self):
        os.remove(os.path.join(self.repo, "go.mod"))
        meta = self.new_session("bare", None)
        self.assertEqual(meta["test_cmds"], [])
        self.assertEqual(self.allow(meta), rules("command", NAV_TANDEM + read_git(meta["repo"])))

    def test_prefix_normalisation(self):
        meta = self.new_session("norm", ["  go   test ", "go test", "make (all)", "tandem send", "make test"])
        self.assertEqual(meta["test_cmds"], ["go test", "make (all)", "tandem send", "make test"])
        # `make (all)` would end the rule early, so it is left out.
        self.assertEqual(self.allow(meta), rules("command", NAV_TANDEM + ["go test", "make test"] + read_git(meta["repo"])))
