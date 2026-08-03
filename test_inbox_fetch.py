#!/usr/bin/env python3
"""inbox_fetch 캡처 라인 파싱 회귀 테스트."""
import unittest

import inbox_fetch


def parse(content):
    m = inbox_fetch.ITEM_RE.match(content)
    if not m:
        return None
    return {"kind": m.group(1), "project": m.group(2), "text": m.group(3)}


class ItemParseTest(unittest.TestCase):
    def test_note_with_project(self):
        r = parse("📥 [note] (haengdong) 도메인 당분간 갱신 안함, 우선순위 낮춰")
        self.assertEqual(r["kind"], "note")
        self.assertEqual(r["project"], "haengdong")
        self.assertIn("우선순위", r["text"])

    def test_note_without_project(self):
        r = parse("📥 [note] 이번 주는 포크레터만")
        self.assertEqual(r["kind"], "note")
        self.assertIsNone(r["project"])

    def test_all_kinds_recognized(self):
        for kind in ("task", "idea", "til", "checkin", "note"):
            r = parse(f"📥 [{kind}] 내용")
            self.assertEqual(r["kind"], kind, kind)

    def test_non_capture_line_ignored(self):
        self.assertIsNone(parse("그냥 일반 메시지"))
        self.assertIsNone(parse("📥 [unknown] x"))

    def test_multiline_text(self):
        r = parse("📥 [note] (forcletter) 첫 줄\n둘째 줄")
        self.assertEqual(r["kind"], "note")
        self.assertIn("둘째 줄", r["text"])


if __name__ == "__main__":
    unittest.main()
