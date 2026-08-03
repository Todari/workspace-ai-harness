import tempfile
import unittest

import harness_lib as lib
import plan_gate

ROOT = lib.WORKSPACE_ROOT
CWD = lib.os.path.join(ROOT, "projects", "demo")


def hook_input(**kw):
    data = {
        "session_id": "test-session",
        "cwd": CWD,
        "tool_name": "Edit",
        "tool_input": {"file_path": CWD + "/src/page.tsx"},
        "permission_mode": "default",
    }
    data.update(kw)
    return data


class PlanGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = lib.MARKER_DIR
        lib.MARKER_DIR = self.tmp.name

    def tearDown(self):
        lib.MARKER_DIR = self.orig
        self.tmp.cleanup()

    def test_denies_source_edit_without_marker(self):
        decision = plan_gate.decide(hook_input())
        out = decision["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("!quick", out["permissionDecisionReason"])
        self.assertIn("deep-plan", out["permissionDecisionReason"])

    def test_allows_outside_workspace(self):
        self.assertIsNone(plan_gate.decide(hook_input(cwd=lib.os.path.dirname(ROOT))))

    def test_allows_with_marker(self):
        lib.set_marker("test-session", "user-bypass")
        self.assertIsNone(plan_gate.decide(hook_input()))

    def test_allows_plan_mode(self):
        self.assertIsNone(plan_gate.decide(hook_input(permission_mode="plan")))

    def test_allows_docs_path(self):
        self.assertIsNone(plan_gate.decide(hook_input(tool_input={
            "file_path": CWD + "/docs/superpowers/plans/example.md"})))

    def test_allows_claude_md(self):
        self.assertIsNone(plan_gate.decide(hook_input(tool_input={
            "file_path": CWD + "/CLAUDE.md"})))

    def test_allows_dot_claude(self):
        self.assertIsNone(plan_gate.decide(hook_input(tool_input={
            "file_path": CWD + "/.claude/settings.json"})))

    def test_allows_scratchpad(self):
        self.assertIsNone(plan_gate.decide(hook_input(tool_input={
            "file_path": "/private/tmp/claude-501/foo/note.md"})))

    def test_allows_home_claude_dir(self):
        self.assertIsNone(plan_gate.decide(hook_input(tool_input={
            "file_path": lib.os.path.expanduser("~/.claude/skills/x/SKILL.md")})))

    def test_denies_notebook_edit(self):
        decision = plan_gate.decide(hook_input(
            tool_name="NotebookEdit",
            tool_input={"notebook_path": CWD + "/nb.ipynb"}))
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
