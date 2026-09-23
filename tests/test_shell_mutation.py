import unittest

from codex_deepseek_team.shell_mutation import classify_shell_mutation


class ShellMutationTests(unittest.TestCase):
    def test_quoted_operators_and_command_names_are_data(self):
        commands = [
            "python3 -c 'assert 2 > 1; assert 2 >= 1'",
            'printf "%s\\n" "a > b"',
            "rg 'sed -i|git apply|tee output' src",
            "printf '%s' 'rm file; touch another'",
            "printf '%s' \\> literal",
            "echo '$(touch hidden)'",
            "echo $((2 > 1))",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(classify_shell_mutation(command), (False, []))

    def test_comments_do_not_execute(self):
        self.assertEqual(classify_shell_mutation("echo safe # touch hidden > output"), (False, []))

    def test_program_heredoc_body_is_not_shell_syntax(self):
        command = "python3 - <<'PY'\nassert 2 > 1\n# touch hidden\nPY\n"
        self.assertEqual(classify_shell_mutation(command), (False, []))

    def test_heredoc_header_and_following_commands_are_classified(self):
        command = "cat <<'EOF' > generated.txt\na > b\nEOF\ntouch after.txt"
        self.assertEqual(classify_shell_mutation(command), (True, ["generated.txt", "after.txt"]))

    def test_multiple_and_tab_stripped_heredocs_are_skipped(self):
        command = "cat <<A <<-'B'\nx > y\nA\n\ttouch hidden\n\tB\n"
        self.assertEqual(classify_shell_mutation(command), (False, []))

    def test_real_redirections_collect_all_literal_destinations(self):
        commands = {
            'printf x > "file with spaces.py"': ["file with spaces.py"],
            'echo x >> a.py 2>error.log': ["a.py", "error.log"],
            'echo x &>combined.log': ["combined.log"],
            'echo x >|forced.txt': ["forced.txt"],
            'echo x <>created.txt': ["created.txt"],
            '>empty.txt': ["empty.txt"],
            'echo x >output.txt 2>&1': ["output.txt"],
        }
        for command, paths in commands.items():
            with self.subTest(command=command):
                self.assertEqual(classify_shell_mutation(command), (True, paths))

    def test_descriptor_duplication_and_input_are_not_writes(self):
        for command in ("echo x 1>&2", "echo x 2>&-", "cat <input.txt", "cat <<< 'x > y'"):
            with self.subTest(command=command):
                self.assertEqual(classify_shell_mutation(command), (False, []))

    def test_coordination_command_does_not_exempt_chained_writes(self):
        commands = [
            "deepseek-team coordination show; touch unrelated.py",
            "deepseek-team worker && echo x > unrelated.py",
            "printf '%s' 'deepseek-team coordination' | tee unrelated.py",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(classify_shell_mutation(command), (True, ["unrelated.py"]))

    def test_supported_commands_collect_their_changed_paths(self):
        commands = {
            "rm -rf -- old.py old_dir": ["old.py", "old_dir"],
            'touch a.py "b c.py"': ["a.py", "b c.py"],
            "cp original.py copied.py": ["copied.py"],
            "mv old.py new.py": ["old.py", "new.py"],
            "truncate -s 0 data.txt": ["data.txt"],
            "printf x | tee -a first.txt second.txt": ["first.txt", "second.txt"],
            "touch a.py\nrm b.py": ["a.py", "b.py"],
            "VALUE=x /usr/bin/touch a.py": ["a.py"],
        }
        for command, paths in commands.items():
            with self.subTest(command=command):
                self.assertEqual(classify_shell_mutation(command), (True, paths))

    def test_complex_writes_never_return_a_partial_scope(self):
        commands = [
            "touch known.py; sed -i.bak 's/a/b/' another.py",
            "echo x > known.py; git apply change.patch",
            "echo x > known.py; patch -p1 < changes.patch",
            "touch known.py; cp --backup source.py target.py",
            "touch known.py; touch \"$TARGET\"",
            "touch known.py; rm *.py",
            "cd nested && touch relative.py",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(classify_shell_mutation(command), (True, []))

    def test_actual_command_substitutions_are_examined(self):
        commands = {
            'printf "%s" "$(touch nested.py)"': ["nested.py"],
            'echo `touch backtick.py`': ["backtick.py"],
            'echo "$(printf \'%s\' \'>\')"': [],
        }
        for command, paths in commands.items():
            with self.subTest(command=command):
                self.assertEqual(classify_shell_mutation(command), (bool(paths), paths))

    def test_dynamic_destination_discards_even_known_nested_scope(self):
        self.assertEqual(classify_shell_mutation('touch "$(touch nested.py; echo outer.py)"'), (True, []))


    def test_compound_commands_do_not_hide_a_second_write(self):
        commands = [
            "touch known.py; if true; then touch other.py; fi",
            "touch known.py; { rm other.py; }",
            "touch known.py; for item in a b; do touch other.py; done",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(classify_shell_mutation(command), (True, []))

    def test_unquoted_heredoc_expands_commands_but_quoted_body_does_not(self):
        self.assertEqual(classify_shell_mutation("cat <<EOF\n$(touch nested.py)\na > b\nEOF\n"),
                         (True, ["nested.py"]))
        self.assertEqual(classify_shell_mutation("cat <<'EOF'\n$(touch nested.py)\na > b\nEOF\n"),
                         (False, []))

    def test_escaped_literal_paths_and_multiple_redirections_keep_scope(self):
        self.assertEqual(classify_shell_mutation("touch 'literal*.py' a\\ b.py > output.txt"),
                         (True, ["output.txt", "literal*.py", "a b.py"]))


    def test_non_shell_whitespace_does_not_stall_the_lexer(self):
        import json
        import subprocess
        import sys

        script = (
            "import json; from codex_deepseek_team.shell_mutation import classify_shell_mutation; "
            "print(json.dumps(classify_shell_mutation('touch odd\\vname.py')))"
        )
        try:
            completed = subprocess.run([sys.executable, "-c", script], check=True,
                                       capture_output=True, text=True, timeout=2)
        except subprocess.TimeoutExpired:
            self.fail("The classifier stalled on a non-shell whitespace character.")
        self.assertEqual(json.loads(completed.stdout), [True, ["odd\vname.py"]])


    def test_bash_comparisons_are_not_redirections(self):
        commands = [
            "[[ 2 > 1 ]]",
            "[[ a < b && c > b ]]",
            "((2 > 1))",
            "(( (3 > 2) && (2 >= 1) ))",
            "[[ ']]' > a ]]",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(classify_shell_mutation(command), (False, []))

    def test_comparison_expansions_and_following_redirects_still_execute(self):
        cases = {
            '[[ "$(touch nested.py; echo 2)" > 1 ]]': (True, ["nested.py"]),
            "(( $(touch nested.py; echo 2) > 1 ))": (True, ["nested.py"]),
            "[[ 2 > 1 ]] > output.txt": (True, ["output.txt"]),
            "((2 > 1)) > output.txt": (True, ["output.txt"]),
            "printf [[ 2 > output.txt ]]": (True, ["output.txt"]),
            "if [[ 2 > 1 ]]; then echo yes; fi": (False, []),
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(classify_shell_mutation(command), expected)

    def test_command_execution_options_preserve_write_detection(self):
        for prefix in ("command -p", "command --", "command -p --"):
            with self.subTest(prefix=prefix):
                self.assertEqual(classify_shell_mutation(prefix + " touch hidden.py"),
                                 (True, ["hidden.py"]))

    def test_command_introspection_does_not_execute_named_commands(self):
        for flag in ("-v", "-V", "-pv", "-pV"):
            with self.subTest(flag=flag):
                self.assertEqual(classify_shell_mutation("command " + flag + " touch hidden.py"),
                                 (False, []))
        self.assertEqual(classify_shell_mutation("command -v touch > commands.txt"),
                         (True, ["commands.txt"]))
        self.assertEqual(classify_shell_mutation('command -v "$(touch nested.py; echo touch)"'),
                         (True, ["nested.py"]))

    def test_read_only_sed_and_git_do_not_mutate(self):
        for command in ("sed 's/a/b/' file.py", "git diff --name-only", "git status --short"):
            with self.subTest(command=command):
                self.assertEqual(classify_shell_mutation(command), (False, []))


if __name__ == "__main__":
    unittest.main()
