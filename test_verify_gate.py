import os
import tempfile
import unittest

import harness_lib as lib
import verify_gate

CWD = os.path.join(lib.WORKSPACE_ROOT, "projects", "demo")


def ev(event, **kw):
    data = {
        "session_id": "vg-test",
        "cwd": CWD,
        "hook_event_name": event,
    }
    data.update(kw)
    return data


class VerifyGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_dir = verify_gate.VERIFY_DIR
        verify_gate.VERIFY_DIR = self.tmp.name

    def tearDown(self):
        verify_gate.VERIFY_DIR = self.orig_dir
        self.tmp.cleanup()

    def edit(self, path=CWD + "/src/page.tsx"):
        return verify_gate.handle(ev(
            "PostToolUse", tool_name="Edit", tool_input={"file_path": path}))

    def bash(self, command, **kw):
        return verify_gate.handle(ev(
            "PostToolUse", tool_name="Bash", tool_input={"command": command}, **kw))

    def stop(self, **kw):
        return verify_gate.handle(ev("Stop", **kw))

    def test_edit_without_verify_blocks_stop(self):
        self.edit()
        decision = self.stop()
        self.assertEqual(decision["decision"], "block")
        self.assertIn("검증", decision["reason"])

    def test_same_edit_blocks_only_once_across_turns(self):
        self.edit()
        self.assertEqual(self.stop()["decision"], "block")
        self.assertIsNone(self.stop())

    def test_new_edit_after_prompt_blocks_once_again(self):
        self.edit()
        self.assertEqual(self.stop()["decision"], "block")
        self.edit(path=CWD + "/src/other.ts")
        self.assertEqual(self.stop()["decision"], "block")

    def test_edit_then_verify_allows_stop(self):
        self.edit()
        self.bash("pnpm run type-check")
        self.assertIsNone(self.stop())

    def test_no_edits_allows_stop(self):
        self.assertIsNone(self.stop())

    def test_stop_hook_active_allows_stop(self):
        self.edit()
        self.assertIsNone(self.stop(stop_hook_active=True))

    def test_docs_edit_not_tracked(self):
        self.edit(path=CWD + "/docs/plans/x.md")
        self.assertIsNone(self.stop())

    def test_root_markdown_and_agents_edit_not_tracked(self):
        for path in (CWD + "/README.md", CWD + "/AGENTS.md"):
            with self.subTest(path=path):
                self.edit(path=path)
                self.assertIsNone(self.stop())

    def test_outside_workspace_ignored(self):
        verify_gate.handle(ev("PostToolUse", cwd="/elsewhere",
                              tool_name="Edit",
                              tool_input={"file_path": "/elsewhere/a.ts"}))
        self.assertIsNone(self.stop(cwd="/elsewhere"))

    def test_external_path_from_workspace_cwd_is_ignored(self):
        self.edit(path="/etc/example.ts")
        self.assertIsNone(self.stop())

    def test_verify_before_edit_still_blocks(self):
        self.bash("npx tsc --noEmit")
        self.edit()
        self.assertEqual(self.stop()["decision"], "block")

    def test_failed_verify_output_not_counted(self):
        # `tsc || true` 류 합성 명령: 전체 exit 0이라 PostToolUse는 발화하지만 출력에 실패 흔적
        self.edit()
        self.bash("npx tsc --noEmit || true", tool_response={
            "stdout": "src/a.ts(3,1): error TS2304: Cannot find name 'x'.",
            "stderr": "", "interrupted": False})
        self.assertEqual(self.stop()["decision"], "block")

    def test_interrupted_verify_not_counted(self):
        self.edit()
        self.bash("npx tsc --noEmit", tool_response={
            "stdout": "", "stderr": "", "interrupted": True})
        self.assertEqual(self.stop()["decision"], "block")

    def test_clean_verify_response_counted(self):
        self.edit()
        self.bash("npx tsc --noEmit", tool_response={
            "stdout": "", "stderr": "", "interrupted": False})
        self.assertIsNone(self.stop())

    def test_missing_tool_response_counts_fail_open(self):
        self.edit()
        self.bash("npx tsc --noEmit")  # tool_response 없음 — 판단 불가 시 카운트
        self.assertIsNone(self.stop())

    def test_codex_apply_patch_is_tracked(self):
        verify_gate.handle(ev(
            "PostToolUse", tool_name="apply_patch",
            tool_input={"patch": "*** Update File: src/page.tsx\n@@\n-x\n+y"}))
        self.assertEqual(self.stop()["decision"], "block")

    def test_codex_canonical_apply_patch_command_is_tracked(self):
        verify_gate.handle(ev(
            "PostToolUse", tool_name="apply_patch",
            tool_input={"command": "*** Update File: src/page.tsx\n@@\n-x\n+y"}))
        self.assertEqual(self.stop()["decision"], "block")

    def test_codex_docs_only_patch_is_not_tracked(self):
        verify_gate.handle(ev(
            "PostToolUse", tool_name="apply_patch",
            tool_input={"patch": "*** Update File: docs/note.md\n@@\n-x\n+y"}))
        self.assertIsNone(self.stop())

    def test_codex_canonical_docs_only_patch_is_not_tracked(self):
        verify_gate.handle(ev(
            "PostToolUse", tool_name="apply_patch",
            tool_input={"command": "*** Update File: docs/note.md\n@@\n-x\n+y"}))
        self.assertIsNone(self.stop())

    def test_codex_exec_verify_uses_cmd_and_exit_code(self):
        self.edit()
        verify_gate.handle(ev(
            "PostToolUse", tool_name="exec_command",
            tool_input={"cmd": "pnpm run type-check"},
            tool_response={"exit_code": 0, "output": ""}))
        self.assertIsNone(self.stop())

    def test_codex_failed_exec_verify_not_counted(self):
        self.edit()
        verify_gate.handle(ev(
            "PostToolUse", tool_name="exec_command",
            tool_input={"cmd": "pnpm run type-check"},
            tool_response={"exit_code": 1, "output": "failed"}))
        self.assertEqual(self.stop()["decision"], "block")

    def test_codex_string_failure_marker_not_counted(self):
        self.edit()
        verify_gate.handle(ev(
            "PostToolUse", tool_name="Bash",
            tool_input={"command": "npx tsc --noEmit || true"},
            tool_response="src/a.ts(3,1): error TS2304: missing name"))
        self.assertEqual(self.stop()["decision"], "block")

    def test_thread_id_fallback(self):
        data = ev("PostToolUse", tool_name="Edit",
                  tool_input={"file_path": CWD + "/src/page.tsx"})
        data.pop("session_id")
        data["thread_id"] = "codex-thread"
        verify_gate.handle(data)
        stop = ev("Stop")
        stop.pop("session_id")
        stop["thread_id"] = "codex-thread"
        self.assertEqual(verify_gate.handle(stop)["decision"], "block")


class IsVerifyCommandTest(unittest.TestCase):
    def test_positive(self):
        for cmd in ("npx tsc --noEmit", "pnpm run type-check", "npm run verify", "npm test",
                    "yarn build", "turbo run lint", "python3 -m pytest",
                    "cd api && go test ./...", "pnpm vitest run"):
            self.assertTrue(verify_gate.is_verify_command(cmd), cmd)

    def test_negative(self):
        for cmd in ("ls -la", "git status", "echo test로 시작", "npm install",
                    "npm run smoke", "echo pytest", "rg type-check package.json",
                    "printf 'npm test'"):
            self.assertFalse(verify_gate.is_verify_command(cmd), cmd)

    def test_cross_stack_positive(self):
        for cmd in ("./gradlew test", "mvn verify", "flutter analyze",
                    "cargo check", "dotnet test", "ruff check .",
                    "pnpm --filter api run type-check",
                    "npm -w apps/web run build"):
            self.assertTrue(verify_gate.is_verify_command(cmd), cmd)


if __name__ == "__main__":
    unittest.main()


class NewlineSegmentTest(unittest.TestCase):
    """줄바꿈은 명령 구분자다 — 여러 줄 스크립트의 검증 명령을 놓치면 게이트가 오탐한다."""

    def test_verify_on_second_line(self):
        self.assertTrue(verify_gate.is_verify_command(
            "cd /tmp\npython3 -m unittest discover 2>&1 | tail -3"))

    def test_verify_after_echo_lines(self):
        self.assertTrue(verify_gate.is_verify_command(
            'cd /tmp\necho "=== 테스트 ==="\nnpm test'))

    def test_plain_lines_are_not_verification(self):
        self.assertFalse(verify_gate.is_verify_command("cd /tmp\nls -la"))

    def test_heredoc_body_is_not_shell(self):
        """heredoc 본문의 pytest는 하위 프로세스 입력이지 실행된 검증이 아니다."""
        self.assertFalse(verify_gate.is_verify_command(
            "cat > f.py <<'EOF'\nsubprocess.run(['pytest'])\nEOF"))

    def test_parsing_resumes_after_heredoc_delimiter(self):
        """heredoc은 종료 구분자에서 끝난다 — 그 다음 줄은 다시 셸 명령이다."""
        self.assertTrue(verify_gate.is_verify_command(
            "cat >> t.py <<'EOF'\nclass T: pass\nEOF\npython3 -m unittest discover"))
        self.assertTrue(verify_gate.is_verify_command(
            "python3 - <<'PYEOF'\nprint(1)\nPYEOF\nnpm test"))

    def test_indented_heredoc_delimiter(self):
        self.assertFalse(verify_gate.is_verify_command(
            "cat <<-EOF\n\tnpm test\n\tEOF\nls"))

    def test_herestring_is_not_heredoc(self):
        """`<<<`는 본문이 없다 — 다음 줄을 건너뛰면 안 된다."""
        self.assertTrue(verify_gate.is_verify_command(
            "grep x <<<'hello'\npython3 -m unittest discover"))


class EditAndVerifyTogetherTest(unittest.TestCase):
    """한 호출이 편집과 검증을 함께 하면 둘 다 기록해야 검증이 묻히지 않는다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = verify_gate.VERIFY_DIR
        verify_gate.VERIFY_DIR = self.tmp.name

    def tearDown(self):
        verify_gate.VERIFY_DIR = self.orig
        self.tmp.cleanup()

    def _post(self, command):
        verify_gate.handle(ev("PostToolUse", tool_name="Bash",
                              tool_input={"command": command},
                              tool_response={"exit_code": 0}))
        return verify_gate.load_state("vg-test")

    def test_write_then_test_records_both(self):
        state = self._post(f"echo x > {CWD}/src/a.ts && npm test")
        self.assertTrue(state.get("last_edit"), "편집이 기록돼야 한다")
        self.assertTrue(state.get("last_verify"), "같은 호출의 검증도 기록돼야 한다")

    def test_commit_with_coauthor_trailer_records_verify_only(self):
        """커밋 트레일러의 <a@b>가 편집으로 오인되면 Stop 게이트가 오탐한다."""
        state = self._post(
            "cd /x\nnpm test\ngit commit -F - <<'EOF'\nfeat: x\n\n"
            "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>\nEOF\ngit push")
        self.assertFalse(state.get("last_edit"), "커밋 메시지는 소스 편집이 아니다")
        self.assertTrue(state.get("last_verify"))
