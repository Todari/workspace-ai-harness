import os
import tempfile
import unittest

import plan_marker
import quick_bypass
import harness_lib as lib

CWD = os.path.join(lib.WORKSPACE_ROOT, "projects", "demo")

GOOD_PLAN = "\n".join(["# 계획", ""] + ["- 단계 %d: src/a.ts 수정" % i for i in range(14)]
                      + ["- 검증: npx tsc --noEmit"])


class PlanQualityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, content):
        path = os.path.join(self.tmp.name, "plan.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_short_plan_is_shallow(self):
        issue = plan_marker.plan_quality_issue(self.write("# 계획\n- 고친다\n"))
        self.assertIsNotNone(issue)
        self.assertIn("줄", issue)

    def test_long_plan_without_verify_is_shallow(self):
        content = "\n".join("- 단계 %d" % i for i in range(20))
        self.assertEqual(plan_marker.plan_quality_issue(self.write(content)),
                         "검증 계획 없음")

    def test_good_plan_passes(self):
        self.assertIsNone(plan_marker.plan_quality_issue(self.write(GOOD_PLAN)))

    def test_unreadable_path_fails_open(self):
        self.assertIsNone(plan_marker.plan_quality_issue(
            self.tmp.name + "/nonexistent.md"))


class PlanMarkerTest(unittest.TestCase):
    def test_exit_plan_mode(self):
        self.assertEqual(
            plan_marker.marker_reason(
                {"cwd": CWD, "tool_name": "ExitPlanMode", "tool_input": {}}),
            "plan-mode-approved")

    def test_plan_doc_write_superpowers(self):
        reason = plan_marker.marker_reason({
            "cwd": CWD, "tool_name": "Write",
            "tool_input": {"file_path": CWD + "/docs/superpowers/plans/2026-07-06-a.md"}})
        self.assertTrue(reason.startswith("plan-doc:"))

    def test_plan_doc_edit_also_marks(self):
        reason = plan_marker.marker_reason({
            "cwd": CWD, "tool_name": "Edit",
            "tool_input": {"file_path": CWD + "/docs/plans/2026-07-06-a.md"}})
        self.assertTrue(reason.startswith("plan-doc:"))

    def test_plan_doc_write_plain(self):
        reason = plan_marker.marker_reason({
            "cwd": CWD, "tool_name": "Write",
            "tool_input": {"file_path": CWD + "/docs/plans/2026-07-06-b.md"}})
        self.assertTrue(reason.startswith("plan-doc:"))

    def test_ordinary_write_is_none(self):
        self.assertIsNone(plan_marker.marker_reason({
            "cwd": CWD, "tool_name": "Write",
            "tool_input": {"file_path": CWD + "/src/a.ts"}}))

    def test_outside_workspace_is_none(self):
        self.assertIsNone(plan_marker.marker_reason(
            {"cwd": "/tmp", "tool_name": "ExitPlanMode", "tool_input": {}}))


class QuickBypassTest(unittest.TestCase):
    def test_token_in_workspace(self):
        self.assertEqual(
            quick_bypass.bypass_reason({"cwd": CWD, "prompt": "!quick 오타 하나만 고쳐줘"}),
            "user-bypass")

    def test_no_token_is_none(self):
        self.assertIsNone(quick_bypass.bypass_reason({"cwd": CWD, "prompt": "고쳐줘"}))

    def test_outside_workspace_is_none(self):
        self.assertIsNone(quick_bypass.bypass_reason({"cwd": "/tmp", "prompt": "!quick"}))

    def test_missing_prompt_is_none(self):
        self.assertIsNone(quick_bypass.bypass_reason({"cwd": CWD}))


if __name__ == "__main__":
    unittest.main()
