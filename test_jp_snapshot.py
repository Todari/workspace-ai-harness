#!/usr/bin/env python3
"""jp_snapshot.write() 회귀 테스트 — 네트워크 없음, 항상 임시 디렉토리에만 쓴다.

fetch()(네트워크/HMAC)는 범위 밖 — write()의 순수 렌더링 로직만 검증한다.
"""
import contextlib
import io
import os
import re
import sys
import tempfile
import unittest

import jp_snapshot

WORDLIST = "단어장.md"
MISTAKES = "틀린 것 모음.md"
HUB = "_일본어.md"


def _read(vault, name):
    with open(os.path.join(vault, name), encoding="utf-8") as f:
        return f.read()


def _row_for(content, prefix):
    """단어장.md 테이블에서 주어진 접두어로 시작하는 행을 찾는다."""
    for line in content.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"{prefix!r} 로 시작하는 행을 찾지 못함: {content!r}")


class WriteThreeFiles(unittest.TestCase):
    def test_creates_expected_files(self):
        data = {
            "cards": [{"front": "食べる", "reading": "たべる", "meaning": "먹다",
                       "example": "ご飯を食べる。", "source": "daily",
                       "createdAt": "2026-07-20T00:00:00.000Z"}],
            "mistakes": [{"original": "私は学生だ", "corrected": "私は学生です",
                          "reason": "정중체 필요", "createdAt": "2026-07-21T00:00:00.000Z"}],
            "stats": {"total": 42, "due": 5, "learned7d": 7},
        }
        with tempfile.TemporaryDirectory() as d:
            jp_snapshot.write(data, vault=d)
            names = set(os.listdir(d))
        self.assertEqual(names, {WORDLIST, MISTAKES, HUB})


class PipeEscaping(unittest.TestCase):
    def test_pipe_and_newline_in_cell_dont_break_table(self):
        data = {
            "cards": [{"front": "美味しい", "reading": "おいしい", "meaning": "맛있다",
                       "example": "これは美味しい|ですね。\n(참고)", "source": "review",
                       "createdAt": "2026-07-22T00:00:00.000Z"}],
            "mistakes": [], "stats": {},
        }
        with tempfile.TemporaryDirectory() as d:
            jp_snapshot.write(data, vault=d)
            content = _read(d, WORDLIST)
        row = _row_for(content, "| 美味しい")
        # 이스케이프된 파이프(문자 자체는 남고 backslash 만 붙는다)는 있어야 하고
        self.assertIn("\\|", row)
        # 개행은 제거돼 한 줄이어야 하고
        self.assertNotIn("\n", row)
        # 진짜 열 구분자(backslash 로 이스케이프되지 않은 '|')는 6열 = 7개여야 함 —
        # 예문 속 '|'가 이스케이프 안 됐다면 여기 8개가 잡혀 표가 깨졌다는 뜻이 된다.
        unescaped_pipes = re.findall(r"(?<!\\)\|", row)
        self.assertEqual(len(unescaped_pipes), 7, row)


class FieldMapping(unittest.TestCase):
    def test_created_at_camelcase_populates_date_column(self):
        data = {
            "cards": [{"front": "食べる", "reading": "たべる", "meaning": "먹다",
                       "example": "ご飯を食べる。", "exampleKo": "밥을 먹다.",
                       "source": "daily", "createdAt": "2026-07-20T00:00:00.000Z"}],
            "mistakes": [], "stats": {},
        }
        with tempfile.TemporaryDirectory() as d:
            jp_snapshot.write(data, vault=d)
            content = _read(d, WORDLIST)
        row = _row_for(content, "| 食べる")
        self.assertIn("2026-07-20", row)
        # 예문 칼럼은 example 이지 exampleKo 가 아니어야 함
        self.assertIn("ご飯を食べる。", row)
        self.assertNotIn("밥을 먹다.", row)

    def test_missing_created_at_leaves_date_blank_not_error(self):
        """실제 응답은 camelCase(createdAt)다 — 과거 브리프 스텁이 가정한
        snake_case(created_at)로는 절대 채워지지 않는다는 걸 못박는 회귀 테스트."""
        data = {
            "cards": [{"front": "x", "reading": "y", "meaning": "z", "example": "e",
                       "source": "s", "created_at": "2026-01-01T00:00:00.000Z"}],
            "mistakes": [], "stats": {},
        }
        with tempfile.TemporaryDirectory() as d:
            jp_snapshot.write(data, vault=d)
            content = _read(d, WORDLIST)
        row = _row_for(content, "| x")
        self.assertTrue(row.endswith("|  |"), row)


class HubStats(unittest.TestCase):
    def test_hub_shows_totals(self):
        data = {"cards": [], "mistakes": [],
                "stats": {"total": 42, "due": 5, "learned7d": 7}}
        with tempfile.TemporaryDirectory() as d:
            jp_snapshot.write(data, vault=d)
            content = _read(d, HUB)
        self.assertIn("총 42", content)
        self.assertIn("복습 대기 5", content)
        self.assertIn("최근 7일 7", content)

    def test_hub_defaults_to_zero_when_stats_missing(self):
        with tempfile.TemporaryDirectory() as d:
            jp_snapshot.write({"cards": [], "mistakes": []}, vault=d)
            content = _read(d, HUB)
        self.assertIn("총 0", content)
        self.assertIn("복습 대기 0", content)
        self.assertIn("최근 7일 0", content)


class MistakesFormatting(unittest.TestCase):
    def test_renders_strikethrough_arrow_bold(self):
        data = {
            "cards": [],
            "mistakes": [{"original": "私は学生だ", "corrected": "私は学生です",
                          "reason": "정중체 필요", "createdAt": "2026-07-21T00:00:00.000Z"}],
            "stats": {},
        }
        with tempfile.TemporaryDirectory() as d:
            jp_snapshot.write(data, vault=d)
            content = _read(d, MISTAKES)
        self.assertIn("~~私は学生だ~~ → **私は学生です**", content)
        self.assertIn("2026-07-21", content)


class SelftestVaultSafety(unittest.TestCase):
    def test_selftest_writes_to_temp_dir_not_real_vault(self):
        argv_backup = sys.argv[:]
        sys.argv = ["jp_snapshot.py", "--selftest"]
        buf = io.StringIO()
        tmp_path = None
        try:
            with contextlib.redirect_stdout(buf):
                rc = jp_snapshot.main()
            output = buf.getvalue()
            self.assertEqual(rc, 0)
            self.assertTrue(output.startswith("selftest: "), output)
            tmp_path = output[len("selftest: "):].split(" ", 1)[0]
            # 실제 볼트(JP_DIR/VAULT_ROOT) 경로가 아니라 임시 디렉토리여야 함
            self.assertNotEqual(tmp_path, jp_snapshot.JP_DIR)
            self.assertFalse(tmp_path.startswith(jp_snapshot.VAULT_ROOT))
            self.assertTrue(os.path.isdir(tmp_path))
            self.assertEqual(
                set(os.listdir(tmp_path)), {WORDLIST, MISTAKES, HUB})
        finally:
            sys.argv = argv_backup
            if tmp_path and os.path.isdir(tmp_path):
                import shutil
                shutil.rmtree(tmp_path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
