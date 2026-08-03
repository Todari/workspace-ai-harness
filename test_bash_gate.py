import os
import tempfile
import unittest

import bash_gate
import harness_lib as lib


class BashGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        self.orig_ws = lib.WORKSPACE_ROOT
        lib.WORKSPACE_ROOT = self.ws
        self.mtmp = tempfile.TemporaryDirectory()
        self.orig_marker = lib.MARKER_DIR
        lib.MARKER_DIR = self.mtmp.name
        os.makedirs(os.path.join(self.ws, "src"))
        with open(os.path.join(self.ws, "src", "a.ts"), "w") as f:
            f.write("export {}\n")

    def tearDown(self):
        lib.WORKSPACE_ROOT = self.orig_ws
        lib.MARKER_DIR = self.orig_marker
        self.tmp.cleanup()
        self.mtmp.cleanup()

    def hook(self, command, **kw):
        data = {
            "session_id": "bg-test",
            "cwd": self.ws,
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "permission_mode": "default",
        }
        data.update(kw)
        return data

    def assert_denied(self, command):
        decision = bash_gate.decide(self.hook(command))
        self.assertIsNotNone(decision, "expected deny: %s" % command)
        self.assertEqual(
            decision["hookSpecificOutput"]["permissionDecision"], "deny")

    def assert_allowed(self, command, **kw):
        self.assertIsNone(bash_gate.decide(self.hook(command, **kw)),
                          "expected allow: %s" % command)

    def test_denies_redirect_to_source(self):
        self.assert_denied("echo x > src/new.ts")

    def test_denies_append_redirect(self):
        self.assert_denied("echo x >> src/a.ts")

    def test_allows_redirect_to_docs(self):
        self.assert_allowed("echo x > docs/note.md")

    def test_allows_redirect_to_tmp(self):
        self.assert_allowed("echo x > /tmp/foo.txt")

    def test_allows_stderr_redirect_to_devnull(self):
        self.assert_allowed("npm run build 2> /dev/null")

    def test_allows_plain_commands(self):
        self.assert_allowed("git status && npm test")

    def test_allows_quoted_greater_than(self):
        self.assert_allowed("python3 -c 'print(3 > 2)'")

    def test_allows_heredoc_body_comparison(self):
        self.assert_allowed("python3 - <<'PY'\nprint(3 > 2)\nPY")

    def test_denies_sed_inplace_on_existing_source(self):
        self.assert_denied("sed -i '' 's/old/new/' src/a.ts")

    def test_allows_sed_inplace_outside_workspace(self):
        self.assert_allowed("sed -i '' 's/a/b/' /etc/hosts")

    def test_denies_tee_to_source(self):
        self.assert_denied("cat patch.txt | tee src/a.ts")

    def test_denies_git_apply(self):
        self.assert_denied("git apply fix.patch")

    def test_allows_variable_target(self):
        self.assert_allowed("echo x > $OUT")

    def test_allows_with_marker(self):
        lib.set_marker("bg-test", "user-bypass")
        self.assert_allowed("echo x > src/new.ts")

    def test_allows_outside_workspace(self):
        self.assert_allowed("echo x > src/new.ts", cwd="/somewhere/else")


if __name__ == "__main__":
    unittest.main()


class HeredocBodyTest(unittest.TestCase):
    """heredoc 본문은 셸 구문이 아니다 — 커밋 메시지가 리다이렉션으로 오인되면 안 된다."""

    def test_coauthor_trailer_is_not_a_redirect(self):
        cwd = os.path.join(lib.WORKSPACE_ROOT, "projects", "demo")
        cmd = (f"cd {cwd}\n"
               "git commit -F - <<'EOF'\n"
               "feat: x\n\n"
               "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>\n"
               "EOF\n"
               "git push")
        self.assertIsNone(
            bash_gate.find_write_target(cmd, cwd))

    def test_redirect_on_heredoc_opening_line_still_detected(self):
        root = lib.WORKSPACE_ROOT
        target_path = os.path.join(root, "projects", "demo", "a.ts")
        cmd = ("cd /x\n"
               f"cat > {target_path} <<'EOF'\n"
               "const a = 1 > 0\n"
               "EOF")
        target = bash_gate.find_write_target(cmd, root)
        self.assertIsNotNone(target)
        self.assertIn("a.ts", target)

    def test_body_redirect_is_ignored(self):
        """본문 안의 `> 파일`은 하위 프로세스 입력이지 셸 리다이렉션이 아니다."""
        target_path = os.path.join(lib.WORKSPACE_ROOT, "projects", "demo", "evil.ts")
        cmd = ("cat <<'EOF'\n"
               f"echo hi > {target_path}\n"
               "EOF")
        self.assertIsNone(bash_gate.find_write_target(cmd, lib.WORKSPACE_ROOT))
