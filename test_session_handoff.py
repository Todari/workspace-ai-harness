import json
import os
import tempfile
import unittest
from unittest import mock

import harness_lib as lib
import session_handoff as sh

CWD = os.path.join(lib.WORKSPACE_ROOT, "projects", "todari")


def _transcript(total):
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    f.write(json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": total, "cache_read_input_tokens": 0}}}) + "\n")
    f.close()
    return f.name


class Handoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        patcher = mock.patch.object(sh, "HANDOFF_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(mock.patch.object(lib, "event", lambda *a, **k: None).start)

    def test_stop_blocks_once_when_context_is_huge(self):
        path = _transcript(600_000)
        data = {"hook_event_name": "Stop", "cwd": CWD, "session_id": "s1", "transcript_path": path}
        first = sh.handle(data)
        self.assertEqual(first["decision"], "block")
        self.assertIn("session_handoff.py write", first["reason"])
        self.assertIn("--session s1", first["reason"])
        self.assertIsNone(sh.handle(data))  # 같은 세션은 다시 막지 않는다
        self.assertIsNone(sh.handle(dict(data, stop_hook_active=True, session_id="s2")))

    def test_stop_is_quiet_below_threshold_or_after_note(self):
        path = _transcript(100_000)
        data = {"hook_event_name": "Stop", "cwd": CWD, "session_id": "s1", "transcript_path": path}
        self.assertIsNone(sh.handle(data))
        sh.write(CWD, "## 목표\nx", session_id="s3")
        big = dict(data, session_id="s3", transcript_path=_transcript(700_000))
        self.assertIsNone(sh.handle(big))

    def test_session_start_injects_once_per_new_session(self):
        sh.write(CWD, "## 목표\n인계 테스트", session_id="old")
        start = {"hook_event_name": "SessionStart", "cwd": CWD, "session_id": "new", "source": "startup"}
        text = sh.handle(start)
        self.assertIn("인계 테스트", text)
        self.assertIn("session_handoff.py done", text)
        self.assertIsNone(sh.handle(start))  # 같은 세션 재주입 없음
        self.assertIsNone(sh.handle(dict(start, session_id="new2", source="compact")))
        self.assertIn("인계 테스트", sh.handle(dict(start, session_id="new2")))
        self.assertIsNone(sh.handle(dict(start, session_id="old")))  # 작성자 세션

    def test_done_and_ttl(self):
        sh.write(CWD, "## 목표\nx", session_id="old")
        _, meta = sh.load(CWD)
        meta["written_at"] -= sh.HANDOFF_TTL_SECONDS + 10
        sh.save_meta(CWD, meta)
        self.assertEqual(sh.load(CWD), (None, {}))
        self.assertTrue(sh.done(CWD))
        self.assertFalse(sh.done(CWD))

    def test_outside_workspace_is_ignored(self):
        self.assertIsNone(sh.handle({"hook_event_name": "SessionStart", "cwd": "/tmp", "source": "startup"}))

    def test_missing_sections(self):
        self.assertEqual(sh.missing_sections("## 목표\n## 결정\n## 진행\n## 검증 상태\n## 다음 할 일\n## 주의"), [])
        self.assertIn("주의", sh.missing_sections("## 목표"))


if __name__ == "__main__":
    unittest.main()
