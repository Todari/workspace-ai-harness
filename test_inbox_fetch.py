#!/usr/bin/env python3
"""inbox_fetch 캡처 라인 파싱 회귀 테스트."""
import contextlib
import io
import json
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
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


class IndexTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = patch.object(inbox_fetch.ops_report, "DB", Path(tmp.name) / "ledger.db")
        db.start()
        self.addCleanup(db.stop)


class PaginationTest(IndexTestCase):
    def setUp(self):
        super().setUp()
        self.messages = {}
        self.requests = []
        api = patch.object(inbox_fetch, "api", side_effect=self.api)
        api.start()
        self.addCleanup(api.stop)

    def feed(self, *messages):
        self.messages.update((int(m["id"]), m) for m in messages)

    def api(self, cfg, method, path):
        self.requests.append(path)
        if path == "/users/@me":
            return {"id": "bot"}
        query = parse_qs(urlsplit(path).query)
        if query:
            after, limit = int(query["after"][0]), int(query["limit"][0])
            ids = sorted(i for i in self.messages if i > after)[:limit]
            return [self.messages[i] for i in reversed(ids)]
        return self.messages[int(path.rsplit("/", 1)[1])]

    def test_resumes_after_two_thousand_messages_and_only_fetches_new_history(self):
        self.feed(*(message(i) for i in range(1, 2103)))
        self.feed(message(2, "📥 [task] 오래된 캡처"), message(2102, "📥 [idea] 새 캡처"))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual([i["id"] for i in inbox_fetch.pending_items(CFG)], ["2"])
        self.requests.clear()
        items = inbox_fetch.pending_items(CFG)
        self.assertEqual([i["id"] for i in items], ["2", "2102"])
        self.assertIn("after=2000", self.requests[1])
        self.requests.clear()
        self.assertEqual(inbox_fetch.pending_items(CFG, count_only=True), 2)
        self.assertEqual(self.requests, ["/users/@me", "/channels/42/messages?limit=100&after=2102"])

    def test_filters_acknowledged_or_foreign_captures_and_preserves_unknown(self):
        unknown = "📥 [future] (lvti) 새 형식\n원문 유지"
        reaction = [{"emoji": {"name": "✅"}, "me": True}]
        self.feed(message(4, "📥 [task] 처리함", reactions=reaction),
                  message(3, "📥 [task] 남의 글", author={"id": "other"}),
                  message(2, unknown), message(1, "📥 [note] (lvti) 수정"))
        items = inbox_fetch.pending_items(CFG)
        self.assertEqual([i["id"] for i in items], ["1", "2"])
        self.assertEqual((items[1]["kind"], items[1]["text"]), ("quarantine", unknown))
        self.messages[1]["reactions"] = reaction
        self.assertEqual([i["id"] for i in inbox_fetch.pending_items(CFG)], ["2"])
        self.assertEqual(inbox_fetch.pending_items(CFG, count_only=True), 1)

    def test_full_page_at_end_is_complete_and_overlaps_deduplicate(self):
        self.feed(*(message(i) for i in range(1, 101)))
        self.feed(message(100, "📥 [task] 보존"))
        self.assertEqual(inbox_fetch.pending_items(CFG, max_pages=1, count_only=True), 1)
        with patch.object(inbox_fetch, "api", side_effect=[{"id": "bot"},
                [self.messages[100], message(101, "📥 [task] 새 항목")]]):
            self.assertEqual(inbox_fetch.pending_items(CFG, count_only=True), 2)

    def test_deleted_original_is_quarantined_without_blocking_other_captures(self):
        self.feed(message(1, "📥 [task] 사라짐"), message(2, "📥 [task] 정상"))
        self.assertEqual(inbox_fetch.pending_items(CFG, count_only=True), 2)
        with patch.object(inbox_fetch, "api", side_effect=[{"id": "bot"}, [],
                          inbox_fetch.InboxError("Discord HTTP 404"), self.messages[2]]):
            missing, valid = inbox_fetch.pending_items(CFG)
        self.assertEqual(missing["reason"], "original_message_missing")
        self.assertEqual(valid["text"], "정상")

    def test_repeated_full_page_does_not_loop(self):
        page = [message(mid) for mid in range(100, 0, -1)]
        with patch.object(inbox_fetch, "api", side_effect=[{"id": "bot"}, page, page]):
            with self.assertRaisesRegex(inbox_fetch.InboxError, "커서"):
                inbox_fetch.pending_items(CFG)

    def test_count_page_failure_is_nonzero_and_checkpoint_survives(self):
        self.feed(*(message(i) for i in range(1, 102)))
        out, err = io.StringIO(), io.StringIO()
        with patch.object(inbox_fetch, "load_env", return_value=CFG), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(inbox_fetch.main(["count", "--max-pages", "1"]), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("상한", err.getvalue())
        self.assertEqual(inbox_fetch.pending_items(CFG, max_pages=1), [])

    def test_long_capture_round_trips_without_sending_bot_token(self):
        content = "📥 [til] " + "긴 본문 " * 1400
        data = content.encode()
        url = "https://cdn.discordapp.com/attachments/42/1/inbox-capture.txt?signature=test"
        self.feed(message(1, inbox_fetch.FILE_ENVELOPE, attachments=[{
            "filename": "inbox-capture.txt", "url": url, "size": len(data)}]))
        with patch.object(inbox_fetch.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(data)
            item, = inbox_fetch.pending_items(CFG)
            self.assertNotIn("Authorization", opener.return_value.open.call_args.args[0].headers)
        self.assertEqual((item["kind"], item["text"]), ("til", content[8:].strip()))
        self.messages[1]["attachments"][0]["url"] = "https://example.com/attachments/42/1"
        item, = inbox_fetch.pending_items(CFG)
        self.assertEqual((item["kind"], item["reason"]), ("quarantine", "invalid_capture_attachment"))
        self.assertEqual(inbox_fetch.pending_items(CFG, count_only=True), 1)


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


class AckTest(IndexTestCase):
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
