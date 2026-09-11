#!/usr/bin/env python3
"""vault_sync(볼트→봇 추출)·inbox_fetch(캡처 파싱) 단위 테스트 — 네트워크 없음."""
import contextlib
import datetime
import io
import os
import subprocess
import sys
import tempfile
import unittest
import unicodedata
from unittest.mock import patch

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

    def test_iso_dates_are_calendar_validated(self):
        for raw in ("2026-02-29", "2026-13-01", "2026-00-04", "2026-2-01", "garbage"):
            self.assertIsNone(vault_sync.normalize_date(raw, TODAY), raw)
        self.assertEqual(vault_sync.normalize_date("2024-02-29", TODAY), "2024-02-29")

    def test_leap_day_rollover_does_not_crash_or_invent_a_date(self):
        self.assertIsNone(vault_sync.normalize_date("2/29", datetime.date(2024, 7, 21)))
        self.assertEqual(vault_sync.normalize_date("2/29", datetime.date(2024, 3, 1)), "2024-02-29")


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
            notes = vault_sync.collect(d, TODAY, registry=[])
        by_name = {n["note"]: n for n in notes}
        self.assertEqual(set(by_name), {"lvti", "이정표"})
        self.assertEqual(by_name["lvti"]["slug"], "lvti")
        self.assertEqual(by_name["이정표"]["slug"], "jeongpyo")
        self.assertEqual(len(by_name["이정표"]["tasks"]), 1)
        self.assertEqual(len(by_name["이정표"]["deadlines"]), 1)

    def test_registry_adds_dedicated_hubs_and_deduplicates_legacy_paths(self):
        registry = [
            {"name": "Forcletter", "path": "linkive/forcletter", "vault_note": "포크레터/포크레터.md"},
            {"name": "sector4", "path": "projects/sector4", "vault_note": "프로젝트/섹터4.md"},
            {"name": "same", "path": "projects/alias", "vault_note": "프로젝트/섹터4.md"},
        ]
        with tempfile.TemporaryDirectory() as d:
            for relative in ("포크레터/포크레터.md", "프로젝트/섹터4.md"):
                actual = os.path.join(d, unicodedata.normalize("NFD", relative))
                os.makedirs(os.path.dirname(actual), exist_ok=True)
                with open(actual, "w", encoding="utf-8") as f:
                    f.write("## 다음 할 일\n- [ ] fixture task\n")
            with patch.object(vault_sync.repos, "load", return_value=registry):
                notes = vault_sync.collect(d, TODAY)
        by_slug = {note["slug"]: note for note in notes}
        self.assertEqual(set(by_slug), {"forcletter", "sector4"})
        self.assertEqual(by_slug["forcletter"]["note"], "포크레터")
        self.assertEqual(len(by_slug["sector4"]["tasks"]), 1)

    def test_explicit_slug_precedes_legacy_name(self):
        self.assertEqual(vault_sync.note_slug("HGT", {"path": "projects/hgt", "vault_slug": "hgt"}), "hgt")
        self.assertEqual(vault_sync.note_slug("lvti", {"path": "projects/lovetype"}), "lvti")

    def test_read_failure_is_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "프로젝트"))
            for name in ("a.md", "b.md"):
                with open(os.path.join(d, "프로젝트", name), "w") as f:
                    f.write("## 다음 할 일\n- [ ] fixture\n")
            with patch.object(vault_sync, "parse_note", side_effect=[([{"text": "first"}], []), PermissionError("private-path")]):
                with self.assertRaises(PermissionError):
                    vault_sync.collect(d, TODAY, registry=[])

    def test_registry_path_cannot_escape_vault(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                vault_sync.collect(d, TODAY, registry=[{
                    "name": "bad", "path": "projects/bad", "vault_note": "../outside.md",
                }])


class PreflightTest(unittest.TestCase):
    revision = "a" * 40

    def output(self, vault, *args):
        if args[0] == "rev-parse":
            return self.revision
        if args[0] == "status":
            return ""
        if args[0] == "branch":
            return "main"
        if args[0] == "config":
            return "origin" if args[-1].endswith(".remote") else "refs/heads/main"
        if args[0] == "ls-remote":
            return self.revision + "\trefs/heads/main"
        raise AssertionError(args)

    def test_checks_actual_remote_and_rechecks_local_state(self):
        with patch.object(vault_sync, "git_output", side_effect=self.output) as git:
            result = vault_sync.preflight("/fixture", self.revision)
        self.assertEqual(result, self.revision)
        self.assertIn(("/fixture", "ls-remote", "--exit-code", "origin", "refs/heads/main"),
                      [call.args for call in git.call_args_list])
        self.assertEqual(sum(call.args[1] == "status" for call in git.call_args_list), 2)

    def test_remote_mismatch_blocks(self):
        def output(vault, *args):
            return "b" * 40 + "\trefs/heads/main" if args[0] == "ls-remote" else self.output(vault, *args)
        with patch.object(vault_sync, "git_output", side_effect=output):
            with self.assertRaisesRegex(vault_sync.PreflightError, "원격 HEAD"):
                vault_sync.preflight("/fixture", self.revision)

    def test_markdown_dirty_blocks_before_remote_access(self):
        def output(vault, *args):
            return "?? 새노트.md\0" if args[0] == "status" else self.output(vault, *args)
        with patch.object(vault_sync, "git_output", side_effect=output) as git:
            with self.assertRaisesRegex(vault_sync.PreflightError, "미커밋 Markdown"):
                vault_sync.preflight("/fixture", self.revision)
        self.assertFalse(any(call.args[1] == "ls-remote" for call in git.call_args_list))

    def test_head_changed_during_collection_blocks(self):
        with patch.object(vault_sync, "git_output", return_value="b" * 40):
            with self.assertRaisesRegex(vault_sync.PreflightError, "수집 중"):
                vault_sync.preflight("/fixture", self.revision)

    def test_git_errors_never_expose_stderr_or_remote(self):
        proc = subprocess.CompletedProcess([], 128, "", "https://private.invalid/secret private-credential")
        with patch.object(vault_sync.subprocess, "run", return_value=proc):
            with self.assertRaises(vault_sync.PreflightError) as raised:
                vault_sync.git_output("/fixture", "ls-remote", "origin", "refs/heads/main")
        self.assertNotIn("private", str(raised.exception))

    def test_git_status_detects_root_and_nested_markdown_only(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "--quiet", d], check=True, capture_output=True)
            with open(os.path.join(d, "local.txt"), "w") as f:
                f.write("not a collected note")
            vault_sync.markdown_clean(d)
            for relative in ("root.md", "nested/note.md"):
                file = os.path.join(d, relative)
                os.makedirs(os.path.dirname(file), exist_ok=True)
                with open(file, "w") as f:
                    f.write("fixture")
                with self.assertRaises(vault_sync.PreflightError):
                    vault_sync.markdown_clean(d)
                os.unlink(file)


class MainTest(unittest.TestCase):
    def test_preview_does_not_contact_git_remote_or_load_credentials(self):
        for flag in ("--dry-run", "--dump"):
            with patch.object(sys, "argv", ["vault_sync.py", flag]), \
                    patch.object(vault_sync.os.path, "isdir", return_value=True), \
                    patch.object(vault_sync, "collect", return_value=[]), \
                    patch.object(vault_sync, "git_output") as git, \
                    patch.object(vault_sync, "load_ops_env") as load, \
                    patch.object(vault_sync, "post") as post, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(vault_sync.main(), 0)
            git.assert_not_called()
            load.assert_not_called()
            post.assert_not_called()

    def test_failed_read_produces_no_partial_output_or_post(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["vault_sync.py", "--dump"]), \
                patch.object(vault_sync.os.path, "isdir", return_value=True), \
                patch.object(vault_sync, "collect", side_effect=PermissionError("private-path")), \
                patch.object(vault_sync, "load_ops_env") as load, \
                patch.object(vault_sync, "post") as post, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(vault_sync.main(), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertNotIn("private-path", err.getvalue())
        load.assert_not_called()
        post.assert_not_called()


class PostTest(unittest.TestCase):
    def test_post_http_error_does_not_print_response_body(self):
        error = vault_sync.urllib.error.HTTPError(
            "https://private.invalid/secret", 500, "private-detail", {}, io.BytesIO(b"private-detail"))
        err = io.StringIO()
        with patch.object(vault_sync.urllib.request, "urlopen", side_effect=error), \
                contextlib.redirect_stderr(err):
            result = vault_sync.post({"notes": []}, {
                "VAULT_SYNC_URL": "https://private.invalid/secret", "VAULT_SYNC_SECRET": "fixture-credential",
            })
        self.assertEqual(result, 1)
        self.assertIn("HTTP 500", err.getvalue())
        self.assertNotIn("private", err.getvalue())

    def test_successful_post_contains_verified_source_revision(self):
        revision = "a" * 40
        with patch.object(sys, "argv", ["vault_sync.py"]), \
                patch.object(vault_sync.os.path, "isdir", return_value=True), \
                patch.object(vault_sync, "git_output", return_value=revision), \
                patch.object(vault_sync, "markdown_clean"), \
                patch.object(vault_sync, "collect", return_value=[]), \
                patch.object(vault_sync, "preflight", return_value=revision) as preflight, \
                patch.object(vault_sync, "load_ops_env", return_value={}), \
                patch.object(vault_sync, "post", return_value=0) as post, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(vault_sync.main(), 0)
        preflight.assert_called_once_with(vault_sync.VAULT, revision)
        self.assertEqual(post.call_args.args[0]["sourceRevision"], revision)

    def test_preflight_failure_does_not_load_credentials_or_post(self):
        with patch.object(sys, "argv", ["vault_sync.py"]), \
                patch.object(vault_sync.os.path, "isdir", return_value=True), \
                patch.object(vault_sync, "git_output", return_value="a" * 40), \
                patch.object(vault_sync, "markdown_clean"), \
                patch.object(vault_sync, "collect", return_value=[]), \
                patch.object(vault_sync, "preflight", side_effect=vault_sync.PreflightError("원격 HEAD 불일치")), \
                patch.object(vault_sync, "load_ops_env") as load, \
                patch.object(vault_sync, "post") as post, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(vault_sync.main(), 1)
        load.assert_not_called()
        post.assert_not_called()


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
