import os
import tempfile
import unittest

import edit_detect
import harness_lib as lib


class FindWriteTargetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        self.orig_ws = lib.WORKSPACE_ROOT
        lib.WORKSPACE_ROOT = self.ws
        os.makedirs(os.path.join(self.ws, "src"))
        with open(os.path.join(self.ws, "src", "a.ts"), "w") as f:
            f.write("export {}\n")

    def tearDown(self):
        lib.WORKSPACE_ROOT = self.orig_ws
        self.tmp.cleanup()

    def hit(self, command):
        return edit_detect.find_write_target(command, self.ws)

    def test_redirect_to_source(self):
        self.assertIn("a.ts", self.hit("echo x >> src/a.ts"))
        self.assertIn("new.ts", self.hit("echo x > src/new.ts"))

    def test_redirect_to_docs_or_tmp_is_not_source(self):
        self.assertIsNone(self.hit("echo x > docs/note.md"))
        self.assertIsNone(self.hit("echo x > /tmp/foo.txt"))
        self.assertIsNone(self.hit("npm run build 2> /dev/null"))

    def test_plain_and_quoted_commands(self):
        self.assertIsNone(self.hit("git status && npm test"))
        self.assertIsNone(self.hit("python3 -c 'print(3 > 2)'"))
        self.assertIsNone(self.hit("python3 - <<'PY'\nprint(3 > 2)\nPY"))

    def test_sed_inplace(self):
        self.assertIn("a.ts", self.hit("sed -i '' 's/old/new/' src/a.ts"))
        self.assertIn("a.ts", self.hit("perl -pi -e 's/old/new/' src/a.ts"))
        self.assertIsNone(self.hit("sed -i '' 's/a/b/' /etc/hosts"))

    def test_tee_and_patch(self):
        self.assertIn("a.ts", self.hit("cat patch.txt | tee src/a.ts"))
        self.assertEqual(self.hit("git apply fix.patch"), "patch/git apply")
        self.assertEqual(self.hit("git -C repo apply fix.patch"), "patch/git apply")
        self.assertIsNone(self.hit("git status && echo apply"))

    def test_home_relative_target(self):
        target = "~/workspace/src/new.ts"
        self.assertEqual(
            edit_detect.resolve(target, self.ws),
            os.path.realpath(os.path.expanduser(target)))

    def test_variable_target_is_unknown(self):
        self.assertIsNone(self.hit("echo x > $OUT"))

    def test_outside_workspace(self):
        self.assertIsNone(edit_detect.find_write_target("echo x > src/new.ts", "/somewhere/else"))


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
        self.assertIsNone(edit_detect.find_write_target(cmd, cwd))

    def test_redirect_on_heredoc_opening_line_still_detected(self):
        root = lib.WORKSPACE_ROOT
        target_path = os.path.join(root, "projects", "demo", "a.ts")
        cmd = ("cd /x\n"
               f"cat > {target_path} <<'EOF'\n"
               "const a = 1 > 0\n"
               "EOF")
        target = edit_detect.find_write_target(cmd, root)
        self.assertIsNotNone(target)
        self.assertIn("a.ts", target)

    def test_body_redirect_is_ignored(self):
        target_path = os.path.join(lib.WORKSPACE_ROOT, "projects", "demo", "evil.ts")
        cmd = ("cat <<'EOF'\n"
               f"echo hi > {target_path}\n"
               "EOF")
        self.assertIsNone(edit_detect.find_write_target(cmd, lib.WORKSPACE_ROOT))


class MetaPathTest(unittest.TestCase):
    def test_meta_paths(self):
        cwd = os.path.join(lib.WORKSPACE_ROOT, "projects", "demo")
        for path in (cwd + "/docs/plans/x.md", cwd + "/CLAUDE.md", cwd + "/.claude/settings.json",
                     "/private/tmp/claude-501/foo/note.md",
                     os.path.expanduser("~/.claude/skills/x/SKILL.md")):
            self.assertTrue(edit_detect.is_meta_path(path), path)

    def test_source_paths(self):
        cwd = os.path.join(lib.WORKSPACE_ROOT, "projects", "demo")
        for path in (cwd + "/src/page.tsx", cwd + "/nb.ipynb", ""):
            self.assertFalse(edit_detect.is_meta_path(path), path)

    def test_target_path_reads_notebook(self):
        self.assertEqual(edit_detect.target_path({"notebook_path": "/a.ipynb"}), "/a.ipynb")
        self.assertEqual(edit_detect.target_path(None), "")


if __name__ == "__main__":
    unittest.main()
