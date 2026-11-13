import os
import re

from tandem import TandemError, maildir, paths, protocol, session, watch
from tests.helpers import TandemTestCase

LIMIT = 22 * 1024
COMBOS = [("driver", "claude"), ("navigator", "agy"), ("navigator", "claude"), ("driver", "agy")]

SHARED_RULES = [
    "tandem recv",
    "tandem send --to",
    "--reply-to",
    "`answer`",
    "risks",
    "strongest\n  objection",
    "At most 3 rounds",
    "escalate",
    "Skip review for trivial changes",
    "A peer agreeing is never user\n  approval",
    "A reply is never promised",
    "say the same in your own console",
    "examples/failures\n  line, not the exit code",
    "checkpoint",
    "--type escalate",
    "tandem human",
    "Never use heredocs",
    "never by full\npath",
    "`tandem:`",
]


def write_instructions(**files):
    folder = paths.instructions_dir()
    os.makedirs(folder, exist_ok=True)
    for name, text in files.items():
        with open(os.path.join(folder, name + ".md"), "w", encoding="utf-8") as f:
            f.write(text)
    return folder


def write_user_guide(name, text):
    os.makedirs(paths.guides_dir(), exist_ok=True)
    with open(os.path.join(paths.guides_dir(), name), "w", encoding="utf-8") as f:
        f.write(text)


class ProtocolTestCase(TandemTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("apix")
        self.meta = session.init_session(
            cwd=self.repo, name="fix-csv-bom", ticket="APIX-123", task="Strip the BOM from CSV uploads.",
            test_cmds=["dip rspec"],
        )
        self.sdir = session.session_path(self.meta["dir"])

    def meta_for(self, role, peer, **changes):
        meta = dict(self.meta, **changes)
        other = "navigator" if role == "driver" else "driver"
        meta["roles"] = {role: peer, other: "agy" if peer == "claude" else "claude"}
        return meta

    def assertClean(self, text):
        self.assertNotIn("{{", text)
        self.assertNotIn("}}", text)
        self.assertNotIn("<!--", text)
        self.assertNotIn("None", text)
        self.assertNotIn("null", text)
        self.assertNotIn("\n\n\n", text)
        self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))


class RenderTest(ProtocolTestCase):
    def test_every_role_and_peer_has_rules_and_doorbell(self):
        for role, peer in COMBOS:
            with self.subTest(role=role, peer=peer):
                text = protocol.render(self.meta_for(role, peer), peer)
                for rule in SHARED_RULES:
                    self.assertIn(rule, text)
                self.assertIn("    " + watch.doorbell(role) + "\n", text)
                self.assertIn("configured tandem to wake you", text)
                self.assertIn("authority from launch", text)
                self.assertIn("You are the **%s**" % role, text)
                self.assertClean(text)

    def test_repo_named_for_every_peer_and_agy_told_to_cd_there(self):
        for role, peer in COMBOS:
            with self.subTest(role=role, peer=peer):
                text = protocol.render(self.meta_for(role, peer), peer)
                self.assertIn("- Repo: `%s`. Run commands there." % self.meta["repo"], text)
                told = "as `cd %s && <command>`" % self.meta["repo"]
                self.assertEqual(told in text, peer == "agy")

    def test_size_under_limit(self):
        for role, peer in COMBOS:
            with self.subTest(role=role, peer=peer):
                text = protocol.render(self.meta_for(role, peer), peer)
                self.assertLess(len(text.encode("utf-8")), LIMIT)

    def test_size_under_limit_with_huge_values(self):
        write_instructions(shared="Ünïcode standing rule. " * 400, driver="Driver rule. " * 400, navigator="Nav. " * 900)
        meta = dict(
            self.meta,
            task="Ünïcode brief line with some words. " * 2000,
            ticket="APIX-" + "9" * 40,
            test_cmds=["bundle exec rspec spec/some/very/long/path_%d_spec.rb" % i for i in range(20)],
        )
        for role, peer in COMBOS:
            with self.subTest(role=role, peer=peer):
                text = protocol.render(self.meta_for(role, peer, **{k: meta[k] for k in ("task", "ticket", "test_cmds")}), peer)
                self.assertLess(len(text.encode("utf-8")), LIMIT)
                self.assertIn("Brief cut at %d bytes" % protocol.TASK_CAP, text)
                self.assertIn(os.path.join(self.sdir, "session.json"), text)

    def test_multibyte_brief_is_cut_by_bytes(self):
        for char in ("日", "😀"):
            text = protocol.render(self.meta_for("navigator", "agy", task=char * 6000), "agy")
            self.assertLess(len(text.encode("utf-8")), LIMIT, char)
            self.assertIn("Brief cut at %d bytes" % protocol.TASK_CAP, text)
            self.assertNotIn("\ufffd", text)

    def test_driver_section(self):
        text = protocol.render(self.meta_for("driver", "claude"), "claude")
        self.assertIn("## Your role: driver", text)
        self.assertNotIn("## Your role: navigator", text)
        self.assertIn("only one who edits", text)
        self.assertIn(os.path.join(self.sdir, "plan.md"), text)
        self.assertIn(os.path.join(self.sdir, "decisions.md"), text)
        self.assertIn("run `git status`", text)
        self.assertIn("--type done", text)
        self.assertIn("run `tandem done`", text)
        self.assertIn("never write the narrative into `summary.md`", text)
        self.assertIn("tandem send --to navigator --type plan", text)

    def test_navigator_section(self):
        text = protocol.render(self.meta_for("navigator", "agy"), "agy")
        self.assertIn("## Your role: navigator", text)
        self.assertNotIn("## Your role: driver", text)
        self.assertIn("Never edit, create or delete files", text)
        self.assertIn("evidence you produced yourself", text)
        self.assertIn("run the tests", text)
        self.assertIn("tandem send --to driver --type review --reply-to <id>", text)
        self.assertNotIn("run `tandem done`", text)

    def test_claude_section(self):
        for role in ("driver", "navigator"):
            text = protocol.render(self.meta_for(role, "claude"), "claude")
            self.assertIn("## Your harness: Claude Code", text)
            self.assertNotIn("## Your harness: agy", text)
            self.assertIn('`Stop hook blocking error from command "Stop": %s`' % watch.doorbell(role), text)
            self.assertIn("not an error", text)
            self.assertNotIn("--detach", text)

    def test_agy_section(self):
        for role in ("driver", "navigator"):
            text = protocol.render(self.meta_for(role, "agy"), "agy")
            self.assertIn("## Your harness: agy", text)
            self.assertNotIn("## Your harness: Claude Code", text)
            self.assertIn("SYSTEM_MESSAGE", text)
            self.assertIn("    tandem watch --as %s --detach\n" % role, text)
            self.assertNotIn("Stop hook blocking error", text)

    def test_session_values(self):
        text = protocol.render(self.meta_for("navigator", "agy"), "agy")
        self.assertIn("# tandem pair session `fix-csv-bom`", text)
        self.assertIn(self.sdir, text)
        self.assertIn("Your peer, Claude Code, is the **driver**", text)
        self.assertIn("You: navigator (agy). Peer: driver (Claude Code).", text)
        self.assertIn("Ticket: `APIX-123`.", text)
        self.assertIn("Strip the BOM from CSV uploads.", text)
        self.assertIn("Test commands: `dip rspec`", text)

    def test_role_defaults_from_session(self):
        text = protocol.render(self.meta, "claude")
        self.assertIn("You are the **driver**", text)
        text = protocol.render(self.meta, "agy")
        self.assertIn("You are the **navigator**", text)

    def test_explicit_role_overrides_session(self):
        text = protocol.render(self.meta, "claude", role="navigator")
        self.assertIn("You are the **navigator**", text)
        self.assertIn("Peer: driver", text)

    def test_loaded_from_session_on_disk(self):
        text = protocol.render(session.load_session(self.meta["dir"]), "agy")
        self.assertIn("fix-csv-bom", text)

    def test_several_test_commands(self):
        meta = dict(self.meta, test_cmds=["go test", "dip rspec"])
        self.assertIn("Test commands: `go test`, `dip rspec`", protocol.render(meta, "claude"))

    def test_placeholders_in_brief_stay_literal(self):
        meta = dict(self.meta, task="Render {{doorbell}} and {{nope}} literally.")
        text = protocol.render(meta, "claude")
        self.assertIn("Render {{doorbell}} and {{nope}} literally.", text)

    def test_template_path_is_package_relative(self):
        self.assertEqual(
            protocol.TEMPLATE,
            os.path.join(os.path.dirname(os.path.abspath(protocol.__file__)), "templates", "protocol.md"),
        )
        os.chdir(self.tmp)
        self.assertIn("tandem recv", protocol.render(self.meta, "claude"))

    def test_template_sections(self):
        selectors = [sel for sel, _ in protocol.load_template()]
        self.assertIn("shared", selectors)
        for name in ("role driver", "role navigator", "peer claude", "peer agy"):
            self.assertEqual(selectors.count(name), 1, name)

    def test_only_known_placeholders(self):
        with open(protocol.TEMPLATE, encoding="utf-8") as f:
            used = set(re.findall(r"\{\{([a-z_]+)\}\}", f.read()))
        self.assertTrue(used)
        self.assertLessEqual(used, set(protocol.values(self.meta, "claude", "driver")))


class CodeValuesTest(ProtocolTestCase):
    def test_message_types_and_reply_defaults_come_from_maildir(self):
        text = protocol.render(self.meta, "claude")
        self.assertIn("- Types: %s." % ", ".join("`%s`" % t for t in maildir.TYPES), text)
        replying = [t for t in maildir.TYPES if t in maildir.EXPECTS_REPLY]
        self.assertIn("%s and `%s` expect a reply." % (", ".join("`%s`" % t for t in replying[:-1]), replying[-1]), text)

    def test_no_mail_exit_comes_from_maildir(self):
        text = protocol.render(self.meta, "agy")
        self.assertIn("`no mail` (exit %d)" % maildir.NO_MAIL, text)
        self.assertIn("exit %d\n  means nothing came" % maildir.NO_MAIL, text)

    def test_launcher_prefix_matches_the_initial_prompts(self):
        text = protocol.render(self.meta, "claude")
        self.assertIn("A prompt starting with `%s` comes from the tandem launcher" % protocol.LAUNCHER_PREFIX, text)
        for role in session.PAIR_ROLES:
            self.assertTrue(protocol.initial_prompt(self.meta, role).startswith(protocol.LAUNCHER_PREFIX))


class MissingValuesTest(ProtocolTestCase):
    def test_no_ticket_no_task(self):
        for role, peer in COMBOS:
            with self.subTest(role=role, peer=peer):
                text = protocol.render(self.meta_for(role, peer, ticket=None, task=None), peer)
                self.assertIn("No ticket or brief was given", text)
                self.assertNotIn("Ticket:", text)
                self.assertNotIn("Brief from the user", text)
                self.assertClean(text)

    def test_ticket_only(self):
        text = protocol.render(dict(self.meta, task=None), "claude")
        self.assertIn("Ticket: `APIX-123`.", text)
        self.assertNotIn("Brief from the user", text)
        self.assertNotIn("No ticket or brief", text)
        self.assertClean(text)

    def test_task_only(self):
        text = protocol.render(dict(self.meta, ticket=None), "claude")
        self.assertNotIn("Ticket:", text)
        self.assertIn("Brief from the user:\n\nStrip the BOM", text)
        self.assertClean(text)

    def test_blank_values_count_as_missing(self):
        text = protocol.render(dict(self.meta, ticket="  ", task="\n\n"), "claude")
        self.assertIn("No ticket or brief was given", text)
        self.assertClean(text)

    def test_no_test_commands(self):
        text = protocol.render(dict(self.meta, test_cmds=[]), "agy")
        self.assertIn("Test commands: none configured", text)
        self.assertClean(text)

    def test_init_without_ticket_or_task(self):
        repo = self.make_repo("admin", branch="feature/x")
        meta = session.init_session(cwd=repo)
        text = protocol.render(meta, "agy")
        self.assertIn("No ticket or brief was given", text)
        self.assertClean(text)


class ErrorTest(ProtocolTestCase):
    def test_unknown_peer(self):
        with self.assertRaises(TandemError):
            protocol.render(self.meta, "copilot")

    def test_bad_role(self):
        with self.assertRaises(TandemError):
            protocol.render(self.meta, "claude", role="human")

    def test_peer_without_role(self):
        meta = dict(self.meta, roles={"driver": "agy", "navigator": "agy"})
        with self.assertRaisesRegex(TandemError, "claude holds no role"):
            protocol.render(meta, "claude")

    def write_template(self, text):
        path = os.path.join(self.tmp, "protocol.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_missing_template(self):
        with self.assertRaisesRegex(TandemError, "cannot read protocol template"):
            protocol.render(self.meta, "claude", path=os.path.join(self.tmp, "absent.md"))

    def test_missing_section(self):
        path = self.write_template("<!-- tandem: shared -->\nx\n<!-- tandem: role driver -->\ny\n")
        with self.assertRaisesRegex(TandemError, "peer claude"):
            protocol.render(self.meta, "claude", path=path)

    def test_unknown_placeholder(self):
        path = self.write_template(
            "<!-- tandem: shared -->\n{{bogus}}\n<!-- tandem: role driver -->\n<!-- tandem: peer claude -->\n"
        )
        with self.assertRaisesRegex(TandemError, "bogus"):
            protocol.render(self.meta, "claude", path=path)

    def test_text_before_first_marker(self):
        path = self.write_template("stray\n<!-- tandem: shared -->\nx\n")
        with self.assertRaisesRegex(TandemError, "before the first section marker"):
            protocol.load_template(path)

    def test_custom_template_order_kept(self):
        path = self.write_template(
            "<!-- tandem: peer claude -->\nC {{role}}\n<!-- tandem: shared -->\nS\n"
            "<!-- tandem: role driver -->\nD\n<!-- tandem: role navigator -->\nN\n<!-- tandem: peer agy -->\nA\n"
        )
        self.assertEqual(protocol.render(self.meta, "claude", path=path), "C driver\n\nS\n\nD\n")


class FinalReviewTest(ProtocolTestCase):
    def test_every_role_and_peer_gets_the_phases(self):
        guides = protocol.session_guides_dir(self.sdir)
        for role, peer in COMBOS:
            with self.subTest(role=role, peer=peer):
                text = protocol.render(self.meta_for(role, peer), peer)
                self.assertIn("## Final review", text)
                self.assertIn("`Final review: yes` when the task changes code", text)
                self.assertIn("`Final review: no`", text)
                self.assertIn(guides, text)
                for name in protocol.guide_sources():
                    self.assertIn("`%s`" % name, text)
                self.assertIn("1. Comprehensive", text)
                self.assertIn("2. Smells", text)
                self.assertIn("3. Critical only", text)
                self.assertIn("`CRITICAL`", text)

    def test_review_range_from_the_base_commit(self):
        base = self.meta["base"]
        self.assertTrue(base)
        self.assertIn("`git diff %s`" % base, protocol.render(self.meta, "agy"))

    def test_review_range_without_a_base(self):
        for meta in (dict(self.meta, base=None), {k: v for k, v in self.meta.items() if k != "base"}):
            text = protocol.render(meta, "agy")
            self.assertIn("every commit since the session started", text)
            self.assertClean(text)

    def test_role_sections(self):
        driver = protocol.render(self.meta_for("driver", "claude"), "claude")
        self.assertIn("open each phase with a `review_request`", driver)
        self.assertIn("after an `APPROVE` with MINOR findings", driver)
        self.assertIn("the `Final review:` line", driver)
        navigator = protocol.render(self.meta_for("navigator", "agy"), "agy")
        self.assertIn("read the phase's lens files first", navigator)
        self.assertIn("grouped by lens", navigator)
        self.assertIn("With only MINOR findings,\n  answer `APPROVE`", navigator)


class InstructionsTest(ProtocolTestCase):
    def test_none_by_default(self):
        self.assertNotIn("## From the user", protocol.render(self.meta, "claude"))

    def test_shared_then_role_at_the_end(self):
        write_instructions(shared="Answer in English.", driver="Run `dip rubocop` before wrap-up.", navigator="Check N+1s.")
        text = protocol.render(self.meta_for("driver", "claude"), "claude")
        self.assertTrue(text.endswith(
            "## From the user\n\nThe user's standing instructions for every tandem session:\n\n"
            "Answer in English.\n\nRun `dip rubocop` before wrap-up.\n"
        ))
        self.assertNotIn("Check N+1s.", text)
        navigator = protocol.render(self.meta_for("navigator", "agy"), "agy")
        self.assertIn("Answer in English.\n\nCheck N+1s.\n", navigator)
        self.assertNotIn("dip rubocop", navigator)
        self.assertClean(text)

    def test_role_file_alone(self):
        write_instructions(navigator="  \nCheck N+1s.\n\n")
        text = protocol.render(self.meta_for("navigator", "agy"), "agy")
        self.assertTrue(text.endswith("for every tandem session:\n\nCheck N+1s.\n"))

    def test_empty_files_add_nothing(self):
        write_instructions(shared="", driver=" \n")
        self.assertNotIn("## From the user", protocol.render(self.meta_for("driver", "claude"), "claude"))

    def test_placeholders_stay_literal(self):
        write_instructions(shared="Keep {{doorbell}} and {{nope}} as typed.")
        self.assertIn("Keep {{doorbell}} and {{nope}} as typed.", protocol.render(self.meta, "claude"))

    def test_cut_at_the_cap(self):
        folder = write_instructions(shared="日" * 3000)
        text = protocol.render(self.meta, "claude")
        self.assertIn("[Instructions cut at %d bytes; the full text is in `%s`.]" % (protocol.INSTRUCTIONS_CAP, folder), text)
        self.assertNotIn("\ufffd", text)

    def test_unreadable_file_is_an_error(self):
        folder = write_instructions()
        with open(os.path.join(folder, "shared.md"), "wb") as f:
            f.write(b"\xff\xfe bad")
        with self.assertRaisesRegex(TandemError, "cannot read .*shared.md"):
            protocol.render(self.meta, "claude")


class GuidesTest(ProtocolTestCase):
    LENSES = {
        "review-quality.md", "review-implementation.md", "review-testing.md",
        "review-simplification.md", "review-documentation.md", "review-smells.md",
    }

    def written(self):
        dest = protocol.write_guides(self.sdir)
        self.assertEqual(dest, protocol.session_guides_dir(self.sdir))
        out = {}
        for name in os.listdir(dest):
            with open(os.path.join(dest, name), encoding="utf-8") as f:
                out[name] = f.read()
        return out

    def test_bundled_lenses(self):
        files = self.written()
        self.assertEqual(set(files), self.LENSES)
        for name, text in files.items():
            self.assertTrue(text.startswith("# Lens: "), name)

    def test_user_guide_replaces_by_name_and_adds_others(self):
        write_user_guide("review-smells.md", "# Our smells\n")
        write_user_guide("rails.md", "# Rails\n")
        write_user_guide("notes.txt", "ignored")
        files = self.written()
        self.assertEqual(set(files), self.LENSES | {"rails.md"})
        self.assertEqual(files["review-smells.md"], "# Our smells\n")
        self.assertEqual(files["rails.md"], "# Rails\n")

    def test_rewrite_drops_guides_the_user_removed(self):
        write_user_guide("rails.md", "# Rails\n")
        self.written()
        os.remove(os.path.join(paths.guides_dir(), "rails.md"))
        self.assertNotIn("rails.md", self.written())
