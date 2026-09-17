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

    def _codex_rollout(self, ctx, total):
        f = tempfile.NamedTemporaryFile("w", prefix="rollout-2026-09-17T00-00-00-abc-", suffix=".jsonl",
                                        delete=False)
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": None}}) + "\n")
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": total},
            "last_token_usage": {"input_tokens": ctx - 100, "output_tokens": 100}}}}) + "\n")
        f.close()
        return f.name

    def test_codex_stop_blocks_on_context_or_cumulative_input(self):
        data = {"hook_event_name": "Stop", "cwd": CWD, "session_id": "c1",
                "transcript_path": self._codex_rollout(120_000, 1_000_000)}
        self.assertIsNone(sh.handle(data))
        big = dict(data, session_id="c2", transcript_path=self._codex_rollout(200_000, 1_000_000))
        res = sh.handle(big)
        self.assertEqual(res["decision"], "block")
        self.assertIn("--client codex", res["reason"])
        self.assertIn("직전 턴 문맥 200k", res["reason"])
        heavy = dict(data, session_id="c3", transcript_path=self._codex_rollout(100_000, 50_000_000))
        self.assertIn("누적 입력 50M", sh.handle(heavy)["reason"])

    def test_codex_transcript_lookup_by_session_id(self):
        with mock.patch.object(sh.context_watch, "CODEX_SESSIONS_DIR", self.tmp):
            d = os.path.join(self.tmp, "2026", "09", "17")
            os.makedirs(d)
            path = os.path.join(d, "rollout-2026-09-17T00-00-00-thread-xyz.jsonl")
            open(path, "w").write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 1}, "last_token_usage": {"input_tokens": 190_000}}}}) + "\n")
            data = {"hook_event_name": "Stop", "cwd": CWD, "thread_id": "thread-xyz"}
            self.assertEqual(sh.handle(data)["decision"], "block")

    def test_precompact_writes_auto_note_but_keeps_manual(self):
        t = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        t.write(json.dumps({"type": "user", "message": {"content": "자동 노트 확인"}}) + "\n"); t.close()
        data = {"hook_event_name": "PreCompact", "cwd": CWD, "session_id": "p1", "transcript_path": t.name}
        self.assertIsNone(sh.handle(data))
        body, meta = sh.load(CWD)
        self.assertTrue(meta["auto"]); self.assertIn("자동 노트 확인", body)
        # 자동 노트만 있으면 Stop은 여전히 수동 노트를 요구한다
        stop = {"hook_event_name": "Stop", "cwd": CWD, "session_id": "p1", "transcript_path": _transcript(600_000)}
        self.assertEqual(sh.handle(stop)["decision"], "block")
        sh.write(CWD, "## 목표\n수동", session_id="p1")
        sh.handle(data)
        body, meta = sh.load(CWD)
        self.assertFalse(meta.get("auto")); self.assertIn("수동", body)
        self.assertIn("자동 생성", sh.inject_text("x", {"auto": True, "written_at": 0}))

    def test_outside_workspace_is_ignored(self):
        self.assertIsNone(sh.handle({"hook_event_name": "SessionStart", "cwd": "/tmp", "source": "startup"}))

    def test_missing_sections(self):
        self.assertEqual(sh.missing_sections("## 목표\n## 결정\n## 진행\n## 검증 상태\n## 다음 할 일\n## 주의"), [])
        self.assertIn("주의", sh.missing_sections("## 목표"))


if __name__ == "__main__":
    unittest.main()
