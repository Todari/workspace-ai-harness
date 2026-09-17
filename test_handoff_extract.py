import json
import os
import tempfile
import unittest

import handoff_extract as hx


def _write(lines, prefix="t-"):
    f = tempfile.NamedTemporaryFile("w", prefix=prefix, suffix=".jsonl", delete=False)
    for row in lines:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    f.close()
    return f.name


class ClaudeExtract(unittest.TestCase):
    def test_collects_prompts_files_and_verify_commands(self):
        path = _write([
            {"type": "user", "message": {"content": "<command-name>/compact</command-name>"}},
            {"type": "user", "message": {"content": "섹터4 실패 원인 고쳐줘"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/a.py"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "python3 -m pytest tests/"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                {"type": "text", "text": "고쳤습니다."}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"},
                                                     {"type": "text", "text": "배포해줘"}]}},
        ])
        ex = hx.from_claude(path)
        self.assertEqual(ex.prompts, ["섹터4 실패 원인 고쳐줘", "배포해줘"])
        self.assertEqual(ex.files, ["/x/a.py"])
        self.assertEqual(ex.commands, ["python3 -m pytest tests/"])
        note = hx.build_note(path, tempfile.gettempdir(), "claude")
        for section in ("## 목표", "## 결정", "## 진행", "## 검증 상태", "## 다음 할 일", "## 주의"):
            self.assertIn(section, note)
        self.assertIn("배포해줘", note)
        os.remove(path)

    def test_empty_transcript_gives_no_note(self):
        path = _write([{"type": "user", "message": {"content": "<local-command-stdout>x"}}])
        self.assertEqual(hx.build_note(path, tempfile.gettempdir()), "")
        self.assertEqual(hx.build_note("/nonexistent.jsonl", tempfile.gettempdir()), "")


class CodexExtract(unittest.TestCase):
    def test_reads_user_messages_and_patches(self):
        patch = "*** Begin Patch\n*** Update File: src/b.ts\n@@\n*** End Patch"
        path = _write([
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                  "content": [{"type": "input_text", "text": "# AGENTS.md instructions"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                  "content": [{"type": "input_text", "text": "봇 수정해줘"}]}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                                                  "input": "apply_patch <<'EOF'\n" + patch + "\nEOF"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "shell",
                                                  "arguments": json.dumps({"command": ["npm", "test"]})}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                  "content": [{"type": "output_text", "text": "완료"}]}},
        ], prefix="rollout-")
        ex = hx.from_codex(path)
        self.assertEqual(ex.prompts, ["봇 수정해줘"])
        self.assertEqual(ex.files, ["src/b.ts"])
        self.assertEqual(ex.commands, ["npm test"])
        self.assertEqual(ex.last_reply, "완료")
        os.remove(path)


if __name__ == "__main__":
    unittest.main()
