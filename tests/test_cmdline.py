import unittest

from tandem import cmdline
from tandem.cmdline import REDIR, SEP, WORD
from tests.helpers import TandemTestCase



def words(text):
    toks, _ = cmdline.tokens(text)
    return [v for k, v in toks if k == WORD]


class TokensTest(TandemTestCase):
    def test_plain_words(self):
        self.assertEqual(cmdline.tokens("go  test\t./..."), ([(WORD, "go"), (WORD, "test"), (WORD, "./...")], False))

    def test_single_quotes_are_literal(self):
        body = "a; b && c | d > e < f $(g) `h` \\ \" ( )\nsecond line"
        toks, expands = cmdline.tokens("tandem send '%s'" % body)
        self.assertEqual(toks, [(WORD, "tandem"), (WORD, "send"), (WORD, body)])
        self.assertFalse(expands)

    def test_adjacent_quotes_join_one_word(self):
        self.assertEqual(words("'don'\\''t' \"x\"'y'z"), ["don't", "xyz"])

    def test_empty_quotes_are_a_word(self):
        self.assertEqual(words("a '' \"\""), ["a", "", ""])

    def test_double_quotes(self):
        toks, expands = cmdline.tokens('echo "a; b | c > d"')
        self.assertEqual(toks, [(WORD, "echo"), (WORD, "a; b | c > d")])
        self.assertFalse(expands)

    def test_double_quote_escapes(self):
        toks, expands = cmdline.tokens(r'echo "a \$HOME \" \\ \x"')
        self.assertEqual(toks, [(WORD, "echo"), (WORD, 'a $HOME " \\ \\x')])
        self.assertFalse(expands)

    def test_expansion_in_double_quotes(self):
        for text in ('echo "$HOME"', 'echo "${X}"', 'echo "`id`"', 'echo "$(id)"'):
            self.assertTrue(cmdline.tokens(text)[1], text)

    def test_unquoted_expansion(self):
        for text in ("echo $HOME", "echo `id`", "echo $(id)", "cat <(ls)", "tee >(cat)"):
            self.assertTrue(cmdline.tokens(text)[1], text)

    def test_backslash_escapes_outside_quotes(self):
        toks, expands = cmdline.tokens(r"echo a\;b \$x \|")
        self.assertEqual(toks, [(WORD, "echo"), (WORD, "a;b"), (WORD, "$x"), (WORD, "|")])
        self.assertFalse(expands)

    def test_line_continuation(self):
        self.assertEqual(words("go \\\ntest"), ["go", "test"])

    def test_separators(self):
        toks, _ = cmdline.tokens("a; b && c || d | e |& f & g\nh")
        self.assertEqual(
            [v for k, v in toks if k == SEP], [";", "&&", "||", "|", "|&", "&", "\n"]
        )

    def test_redirects(self):
        toks, _ = cmdline.tokens("a > b >> c 2>&1 &> d &>> e < f << g <<< h <> i >| j 2>k")
        self.assertEqual(
            [v for k, v in toks if k == REDIR],
            [">", ">>", "2>&", "&>", "&>>", "<", "<<", "<<<", "<>", ">|", "2>"],
        )

    def test_comment_is_skipped(self):
        self.assertEqual(words("ls # rm -rf x"), ["ls"])
        self.assertEqual(words("echo a#b"), ["echo", "a#b"])

    def test_unterminated_quotes(self):
        for text in ("echo 'abc", 'echo "abc', 'echo "$(ls)'):
            with self.assertRaises(cmdline.Unreadable):
                cmdline.tokens(text)


class WriteGateTest(TandemTestCase):
    def reason(self, text):
        return cmdline.write_reason(text)

    def test_writers(self):
        for name in sorted(cmdline.WRITERS):
            self.assertEqual(self.reason("%s some/path" % name), name)

    def test_writer_by_path_and_after_assignment(self):
        self.assertEqual(self.reason("/bin/rm -f x"), "rm")
        self.assertEqual(self.reason("LC_ALL=C touch x"), "touch")

    def test_wrappers(self):
        self.assertEqual(self.reason("xargs -0 rm"), "rm")
        self.assertEqual(self.reason("env -i FOO=1 mv a b"), "mv")
        self.assertEqual(self.reason("timeout 5 cp a b"), "cp")
        self.assertEqual(self.reason("nohup tee log"), "tee")
        self.assertEqual(self.reason("! touch x"), "touch")

    def test_wrapper_options_with_values(self):
        for text in (
            "sudo -u bob rm x",
            "sudo -g staff -u bob rm x",
            "nice -n 5 rm x",
            "nice -5 rm x",
            "timeout -s KILL 5 rm x",
            "timeout --signal KILL -k 2 5 rm x",
            "xargs -I {} rm {}",
            "xargs -n 1 -P 4 rm",
            "env -u HOME rm x",
            "doas -u root rm x",
        ):
            self.assertEqual(self.reason(text), "rm", text)

    def test_heredoc_body_is_read_as_commands(self):
        # Known false positive (docs/backlog/agy-write-gate-misses-indirect-writes.md): the lexer has
        # no heredocs, so a body line starting with a writer asks needlessly.
        self.assertEqual(self.reason("cat <<EOF\nrm x\nEOF"), "rm")

    def test_each_segment(self):
        self.assertEqual(self.reason("ls; touch x"), "touch")
        self.assertEqual(self.reason("ls && mkdir d"), "mkdir")
        self.assertEqual(self.reason("false || rm x"), "rm")
        self.assertEqual(self.reason("git log | tee out"), "tee")
        self.assertEqual(self.reason("ls\nrm x"), "rm")
        self.assertEqual(self.reason("sleep 1 & rm x"), "rm")

    def test_substitutions_and_subshells(self):
        self.assertEqual(self.reason("echo $(touch x)"), "touch")
        self.assertEqual(self.reason('echo "$(rm -rf y)"'), "rm")
        self.assertEqual(self.reason("echo `ln -s a b`"), "ln")
        self.assertEqual(self.reason("(cd sub; chmod +x f)"), "chmod")
        self.assertEqual(self.reason("{ ls; rmdir d; }"), "rmdir")

    def test_quoted_text_is_not_a_command(self):
        for text in ("echo 'rm -rf x'", 'echo "touch a; mv b"', "grep -n 'git commit' README.md", "echo rm"):
            self.assertIsNone(self.reason(text), text)

    def test_output_redirects(self):
        self.assertEqual(self.reason("echo hi > notes.txt"), "redirect > notes.txt")
        self.assertEqual(self.reason("echo hi >>notes.txt"), "redirect >> notes.txt")
        self.assertEqual(self.reason("ls 2> err.log"), "redirect 2> err.log")
        self.assertEqual(self.reason("ls &> all.log"), "redirect &> all.log")
        self.assertEqual(self.reason("ls >| f"), "redirect >| f")
        self.assertEqual(self.reason("cat <> f"), "redirect <> f")
        self.assertEqual(self.reason("ls >&out.log"), "redirect >& out.log")
        self.assertEqual(self.reason("cat <<EOF > f\nhello\nEOF"), "redirect > f")

    def test_harmless_redirects(self):
        for text in (
            "go vet ./... 2>&1",
            "ls >/dev/null",
            "ls 2>/dev/null",
            "ls > /dev/null 2>&1",
            "ls &>/dev/null",
            "echo x >&2",
            "ls 1>&-",
            "wc -l < f",
            "cat <<< text",
            "echo 'a > b'",
            "echo a\\>b",
        ):
            self.assertIsNone(self.reason(text), text)

    def test_sed_in_place(self):
        for text in ("sed -i s/a/b/ f", "sed -i.bak s/a/b/ f", "sed -ni p f", "sed -E -i '' s/a/b/ f",
                     "sed --in-place s/a/b/ f", "sed --in-place=.bak s/a/b/ f", "sed s/a/b/ -i f"):
            self.assertEqual(self.reason(text), "sed -i", text)

    def test_sed_without_in_place(self):
        for text in ("sed -n 1,5p f", "sed -e s/-i// f", "sed -E 's/x/y/' f", "sed -f script.sed f", "sed -f -i f"):
            self.assertIsNone(self.reason(text), text)

    def test_perl_in_place(self):
        for text in ("perl -pi -e s/a/b/ f", "perl -i.bak -pe 1 f", "perl -i -pe 1 f"):
            self.assertEqual(self.reason(text), "perl -i", text)

    def test_perl_without_in_place(self):
        for text in ("perl -Ilib -e 1", "perl -ne print f", "perl -e -i", "perl -le 'print 1'"):
            self.assertIsNone(self.reason(text), text)

    def test_git_writers(self):
        for sub in sorted(cmdline.GIT_WRITERS):
            self.assertEqual(self.reason("git %s some thing" % sub), "git %s" % sub, sub)

    def test_git_writers_added_with_their_read_forms(self):
        writes = {
            "git bisect start": "git bisect",
            "git remote add origin url": "git remote",
            "git remote set-url origin url": "git remote",
            "git notes add -m x": "git notes",
            "git replace a b": "git replace",
            "git symbolic-ref HEAD refs/heads/x": "git symbolic-ref",
            "git symbolic-ref -d HEAD": "git symbolic-ref",
            "git sparse-checkout set dir": "git sparse-checkout",
            "git config user.name x": "git config",
            "git config --unset user.name": "git config",
            "git config set user.name x": "git config",
            "git format-patch HEAD~1": "git format-patch",
            "git archive -o out.tar HEAD": "git archive",
            "git archive --output=out.tar HEAD": "git archive",
            "git worktree add ../x": "git worktree",
        }
        for text, reason in writes.items():
            self.assertEqual(self.reason(text), reason, text)
        reads = (
            "git bisect log",
            "git remote",
            "git remote -v",
            "git remote show origin",
            "git notes",
            "git notes show HEAD",
            "git replace -l",
            "git symbolic-ref HEAD",
            "git sparse-checkout list",
            "git config user.name",
            "git config --get user.name",
            "git config -l",
            "git config --file .gitmodules submodule.x.url",
            "git format-patch --stdout HEAD~1",
            "git archive HEAD",
            "git worktree list",
        )
        for text in reads:
            self.assertIsNone(self.reason(text), text)

    def test_git_global_options(self):
        self.assertEqual(self.reason("git -C /repo commit -m x"), "git commit")
        self.assertEqual(self.reason("git -c user.name=x --no-pager reset --hard"), "git reset")
        self.assertEqual(self.reason("git --git-dir=.git checkout main"), "git checkout")
        self.assertIsNone(self.reason("git -C /repo status"))

    def test_git_read_only(self):
        for text in (
            "git status",
            "git diff HEAD~1",
            "git log --oneline",
            "git show abc123",
            "git blame f",
            "git grep commit",
            "git rev-parse HEAD",
            "git stash list",
            "git stash show -p",
            "git tag",
            "git tag -l 'v*'",
            "git branch",
            "git branch -a -v",
            "git branch --list 'feat*'",
            "git branch --contains abc123",
            "git",
            "git --version",
        ):
            self.assertIsNone(self.reason(text), text)

    def test_git_ref_changing_forms(self):
        self.assertEqual(self.reason("git stash"), "git stash")
        self.assertEqual(self.reason("git stash pop"), "git stash")
        self.assertEqual(self.reason("git tag v1.0"), "git tag")
        self.assertEqual(self.reason("git tag -d v1.0"), "git tag")
        self.assertEqual(self.reason("git branch new-feature"), "git branch")
        self.assertEqual(self.reason("git branch -D old"), "git branch")
        self.assertEqual(self.reason("git branch --set-upstream-to=origin/main"), "git branch")

    def test_find(self):
        self.assertEqual(self.reason("find . -name '*.orig' -delete"), "find")
        self.assertEqual(self.reason("find . -name x -exec rm {} ;"), "find")
        self.assertIsNone(self.reason("find . -name '*.go' -exec grep -l x {} +"))

    def test_reads_pass(self):
        for text in ("ls -la", "cat README.md", "grep -rn foo .", "go vet ./...", "python3 -c 'print(1)'", "", "   "):
            self.assertIsNone(self.reason(text), text)

    def test_unreadable_is_not_flagged(self):
        self.assertIsNone(self.reason("echo 'unterminated"))

    def test_redirect_without_a_target_is_gated(self):
        for text in ("echo hi >", "cat > ; ls", "echo > > x"):
            self.assertEqual(self.reason(text), "redirect >", text)

    def test_assignments_and_bare_wrappers_are_not_writes(self):
        for text in ("FOO=1", "sudo", "env -i", "FOO=1 && ls"):
            self.assertIsNone(self.reason(text), text)


if __name__ == "__main__":
    unittest.main()
