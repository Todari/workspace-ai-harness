#!/usr/bin/env python3
"""vault_sync(볼트→봇 추출)·inbox_fetch(캡처 파싱) 단위 테스트 — 네트워크 없음."""
import datetime
import os
import tempfile
import unittest

import inbox_fetch
import vault_sync

TODAY = datetime.date(2026, 7, 21)

NOTE = """---
type: 프로젝트
---

설명.

## 다음 할 일
- [ ] **첫 번째** 할 일 [[링크]] 📅 8/5
- [x] 끝난 일
- [ ]
- [ ] 두 번째 할 일

## 일정
- 📅 2026-08-14 서류 제출 16:00
- 마커 없는 줄은 무시
"""


class NormalizeDate(unittest.TestCase):
    def test_iso_passthrough(self):
        self.assertEqual(vault_sync.normalize_date("2026-08-05", TODAY), "2026-08-05")

    def test_slash_this_year(self):
        self.assertEqual(vault_sync.normalize_date("8/14", TODAY), "2026-08-14")

    def test_slash_rolls_to_next_year(self):
        # 45일 넘게 지난 M/D 는 내년으로
        self.assertEqual(vault_sync.normalize_date("1/5", TODAY), "2027-01-05")

    def test_recent_past_stays(self):
        self.assertEqual(vault_sync.normalize_date("7/1", TODAY), "2026-07-01")

    def test_invalid_date(self):
        self.assertIsNone(vault_sync.normalize_date("2/30", TODAY))


class ParseNote(unittest.TestCase):
    def test_tasks_and_deadlines(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(NOTE)
            tasks, deadlines = vault_sync.parse_note(path, TODAY)
        self.assertEqual(
            [t["text"] for t in tasks], ["첫 번째 할 일 링크", "두 번째 할 일"])
        self.assertEqual(tasks[0]["due"], "2026-08-05")
        self.assertNotIn("due", tasks[1])
        self.assertEqual(deadlines, [{"text": "서류 제출 16:00", "date": "2026-08-14"}])


class Collect(unittest.TestCase):
    def test_project_dir_and_jeongpyo_merge(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "프로젝트"))
            os.makedirs(os.path.join(d, "이정표", "대회"))
            with open(os.path.join(d, "프로젝트", "lvti.md"), "w", encoding="utf-8") as f:
                f.write("## 다음 할 일\n- [ ] 이미지 수정\n")
            with open(os.path.join(d, "이정표", "이정표.md"), "w", encoding="utf-8") as f:
                f.write("## 다음 할 일\n- [ ] 인터뷰 2건\n")
            with open(os.path.join(d, "이정표", "대회", "모두의 창업.md"), "w",
                      encoding="utf-8") as f:
                f.write("## 일정\n- 📅 2026-08-14 제출\n")
            notes = vault_sync.collect(d, TODAY)
        by_name = {n["note"]: n for n in notes}
        self.assertEqual(set(by_name), {"lvti", "이정표"})
        self.assertEqual(by_name["lvti"]["slug"], "lvti")
        self.assertEqual(by_name["이정표"]["slug"], "jeongpyo")
        self.assertEqual(len(by_name["이정표"]["tasks"]), 1)
        self.assertEqual(len(by_name["이정표"]["deadlines"]), 1)


class InboxItemRe(unittest.TestCase):
    def test_task_with_project(self):
        m = inbox_fetch.ITEM_RE.match("📥 [task] (lvti) 공유 이미지 깨짐")
        self.assertEqual(m.group(1), "task")
        self.assertEqual(m.group(2), "lvti")
        self.assertEqual(m.group(3), "공유 이미지 깨짐")

    def test_idea_without_project(self):
        m = inbox_fetch.ITEM_RE.match("📥 [idea] 메트로놈 진동 모드")
        self.assertEqual(m.group(1), "idea")
        self.assertIsNone(m.group(2))

    def test_til_with_slug(self):
        m = inbox_fetch.ITEM_RE.match("📥 [til] (lvti) 문제→원인→해결 요약")
        self.assertEqual(m.group(1), "til")
        self.assertEqual(m.group(2), "lvti")

    def test_checkin_multiline(self):
        m = inbox_fetch.ITEM_RE.match(
            "📥 [checkin] (2026-07-21) 오늘: 봇 업그레이드\n막힘: -\n내일: 다이제스트 확인")
        self.assertEqual(m.group(1), "checkin")
        self.assertEqual(m.group(2), "2026-07-21")
        self.assertIn("내일: 다이제스트 확인", m.group(3))


if __name__ == "__main__":
    unittest.main()
