#!/usr/bin/env python3
"""inbox_fetch 캡처 라인 파싱 회귀 테스트."""
import contextlib
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import inbox_fetch

CFG = {"DISCORD_BOT_TOKEN": "test-credential", "INBOX_CHANNEL_ID": "42"}


def message(mid, text="일반 알림", **overrides):
    return {"id": str(mid), "author": {"id": "bot"}, "content": text,
            "timestamp": "2026-09-08T01:00:00Z", **overrides}


def http_error(code, body=b"", headers=None):
    return urllib.error.HTTPError(
        "https://discord.com/test", code, "private-server-detail", headers or {}, io.BytesIO(body))


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


class PaginationTest(unittest.TestCase):
    def test_fetches_old_capture_beyond_first_hundred_messages(self):
        first = [message(mid) for mid in range(201, 101, -1)]
        second = [message(101, "📥 [task] (lvti) 오래된 캡처")]
        with patch.object(inbox_fetch, "api", side_effect=[{"id": "bot"}, first, second]) as api:
            items = inbox_fetch.pending_items(CFG)
        self.assertEqual([item["id"] for item in items], ["101"])
        self.assertEqual(api.call_args_list[2].args[2], "/channels/42/messages?limit=100&before=102")

    def test_acknowledged_and_foreign_messages_are_excluded_and_unknown_preserved(self):
        unknown = "📥 [future] (lvti) 새 형식\n원문 유지"
        page = [
            message(4, "📥 [task] 처리함", reactions=[{"emoji": {"name": "✅"}, "me": True}]),
            message(3, "📥 [task] 남의 글", author={"id": "other"}),
            message(2, unknown),
            message(1, "📥 [note] (lvti) 수정"),
        ]
        with patch.object(inbox_fetch, "api", side_effect=[{"id": "bot"}, page]):
            items = inbox_fetch.pending_items(CFG)
        self.assertEqual([item["id"] for item in items], ["1", "2"])
        self.assertEqual(items[1]["kind"], "quarantine")
        self.assertEqual(items[1]["text"], unknown)

    def test_full_page_at_limit_fails_instead_of_returning_partial_items(self):
        first = [message(mid, "📥 [task] 남은 일") for mid in range(100, 0, -1)]
        with patch.object(inbox_fetch, "api", side_effect=[{"id": "bot"}, first]):
            with self.assertRaisesRegex(inbox_fetch.InboxError, "조회 상한"):
                inbox_fetch.pending_items(CFG, max_pages=1)

    def test_repeated_full_page_does_not_loop(self):
        page = [message(mid) for mid in range(100, 0, -1)]
        with patch.object(inbox_fetch, "api", side_effect=[{"id": "bot"}, page, page]):
            with self.assertRaisesRegex(inbox_fetch.InboxError, "커서"):
                inbox_fetch.pending_items(CFG)

    def test_overlap_is_deduplicated_and_result_is_oldest_first(self):
        first = [message(mid, "📥 [task] 항목") for mid in range(201, 101, -1)]
        second = [message(102, "📥 [task] 항목"), message(101, "📥 [task] 항목")]
        with patch.object(inbox_fetch, "api", side_effect=[{"id": "bot"}, first, second]):
            items = inbox_fetch.pending_items(CFG)
        self.assertEqual(len(items), 101)
        self.assertEqual(items[0]["id"], "101")

    def test_count_page_failure_is_nonzero_with_no_zero_output(self):
        first = [message(mid) for mid in range(100, 0, -1)]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(inbox_fetch, "load_env", return_value=CFG), \
                patch.object(inbox_fetch, "api", side_effect=[{"id": "bot"}, first]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = inbox_fetch.main(["count", "--max-pages", "1"])
        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("상한", err.getvalue())


class ApiRetryTest(unittest.TestCase):
    def test_rate_limit_honors_body_and_header_then_succeeds(self):
        error = http_error(429, json.dumps({"retry_after": 1.25}).encode(), {"Retry-After": "1"})
        with patch.object(inbox_fetch.urllib.request, "urlopen", side_effect=[error, io.BytesIO(b'{"ok": true}')]) as request, \
                patch.object(inbox_fetch.time, "sleep") as sleep:
            result = inbox_fetch.api(CFG, "GET", "/test")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1.25)

    def test_5xx_retry_is_bounded_and_safe(self):
        errors = [http_error(503, b"private-server-detail") for _ in range(3)]
        with patch.object(inbox_fetch.urllib.request, "urlopen", side_effect=errors) as request, \
                patch.object(inbox_fetch.time, "sleep") as sleep:
            with self.assertRaises(inbox_fetch.InboxError) as raised:
                inbox_fetch.api(CFG, "PUT", "/test")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertNotIn("private-server-detail", str(raised.exception))
        self.assertNotIn(CFG["DISCORD_BOT_TOKEN"], str(raised.exception))

    def test_nonretryable_http_failure_is_safe(self):
        with patch.object(inbox_fetch.urllib.request, "urlopen", side_effect=http_error(401, b"private-server-detail")) as request, \
                patch.object(inbox_fetch.time, "sleep") as sleep:
            with self.assertRaisesRegex(inbox_fetch.InboxError, "HTTP 401"):
                inbox_fetch.api(CFG, "GET", "/test")
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_long_rate_limit_fails_without_retrying_early(self):
        error = http_error(429, b'{"retry_after": 120}')
        with patch.object(inbox_fetch.urllib.request, "urlopen", side_effect=error) as request, \
                patch.object(inbox_fetch.time, "sleep") as sleep:
            with self.assertRaisesRegex(inbox_fetch.InboxError, "대기 상한"):
                inbox_fetch.api(CFG, "GET", "/test")
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_connection_error_does_not_reveal_reason(self):
        error = urllib.error.URLError("private-server-detail " + CFG["DISCORD_BOT_TOKEN"])
        with patch.object(inbox_fetch.urllib.request, "urlopen", side_effect=error), \
                patch.object(inbox_fetch.time, "sleep"):
            with self.assertRaises(inbox_fetch.InboxError) as raised:
                inbox_fetch.api(CFG, "GET", "/test")
        self.assertNotIn("private-server-detail", str(raised.exception))
        self.assertNotIn(CFG["DISCORD_BOT_TOKEN"], str(raised.exception))


class AckTest(unittest.TestCase):
    def test_partial_failure_attempts_every_unique_id_and_exits_nonzero(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(inbox_fetch, "load_env", return_value=CFG), \
                patch.object(inbox_fetch.ops_report, "receipt", return_value={"disposition": "written"}), \
                patch.object(inbox_fetch, "api", side_effect=[None, inbox_fetch.InboxError("Discord HTTP 403"), None]) as api, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = inbox_fetch.main(["ack", "1,2,3,1"])
        self.assertEqual(rc, 1)
        self.assertEqual(api.call_count, 3)
        self.assertIn("acked 2, failed 1", out.getvalue())
        self.assertIn("ID 2", err.getvalue())

    def test_missing_receipt_refuses_put_but_receipted_id_can_retry(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(inbox_fetch, "load_env", return_value=CFG), \
                patch.object(inbox_fetch.ops_report, "receipt", side_effect=[None, {"disposition": "written"}]), \
                patch.object(inbox_fetch, "api") as api, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = inbox_fetch.main(["ack", "1,2"])
        self.assertEqual(rc, 1)
        self.assertEqual(api.call_count, 1)
        self.assertIn("/messages/2/", api.call_args.args[2])
        self.assertIn("영수증", err.getvalue())

    def test_invalid_ack_id_does_not_load_credentials_or_call_api(self):
        with patch.object(inbox_fetch, "load_env") as load, \
                patch.object(inbox_fetch, "api") as api, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                inbox_fetch.main(["ack", "1,../../bad"])
        self.assertEqual(raised.exception.code, 2)
        load.assert_not_called()
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
