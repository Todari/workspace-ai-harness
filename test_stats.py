import contextlib
import datetime
import io
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import stats

LINES = [
    '{"t":"2026-07-06T10:00:00","kind":"map-inject","session":"aaaa1111","detail":"todari:1200"}',
    '{"t":"2026-07-06T10:01:00","kind":"map-inject","session":"bbbb2222","detail":"todari:800"}',
    '{"t":"2026-07-06T10:02:00","kind":"map-inject","session":"bbbb2222","detail":"linkive"}',
    '{"t":"2026-07-06T10:03:00","kind":"verify-block","session":"bbbb2222","detail":""}',
    'not-json-garbage',
    '{"t":"2020-01-01T00:00:00","kind":"verify-block","session":"old00000","detail":""}',
]


class SummarizeTest(unittest.TestCase):
    def test_counts_kinds_and_sessions(self):
        s = stats.summarize(LINES, since_ts=0)
        self.assertEqual(s["kinds"]["map-inject"], 3)
        self.assertEqual(s["kinds"]["verify-block"], 2)
        self.assertEqual(s["sessions"], 3)

    def test_since_filter_excludes_old(self):
        import time
        cutoff = time.mktime(time.strptime("2026-01-01T00:00:00", "%Y-%m-%dT%H:%M:%S"))
        s = stats.summarize(LINES, since_ts=cutoff)
        self.assertEqual(s["kinds"]["verify-block"], 1)
        self.assertEqual(s["sessions"], 2)

    def test_inject_chars_per_repo_with_legacy_detail(self):
        s = stats.summarize(LINES, since_ts=0)
        self.assertEqual(s["inject_by_repo"][0], ("todari", 2))
        self.assertEqual(s["inject_chars"]["todari"], 2000)
        self.assertNotIn("linkive", s["inject_chars"])  # 문자 수 없는 옛 형식

    def test_garbage_lines_ignored(self):
        s = stats.summarize(["broken", "{}"], since_ts=0)
        self.assertEqual(s["sessions"], 0)


class ComplianceTest(unittest.TestCase):
    def test_counts_prompted_and_verified_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            for name, state in (
                    ("a", {"last_edit": 10, "last_verify": 12, "last_prompted_edit": 10}),
                    ("b", {"last_edit": 10, "last_verify": 0, "last_prompted_edit": 10}),
                    ("c", {"last_edit": 10, "last_verify": 11, "last_prompted_edit": 0}),
            ):
                with open(os.path.join(d, name + ".json"), "w") as f:
                    json.dump(state, f)
            self.assertEqual(stats.verify_compliance(d), (2, 1))

    def test_excludes_state_files_older_than_ttl(self):
        with tempfile.TemporaryDirectory() as d:
            fresh = os.path.join(d, "fresh.json")
            old = os.path.join(d, "old.json")
            for path in (fresh, old):
                with open(path, "w") as f:
                    json.dump({"last_prompted_edit": 10, "last_verify": 0}, f)
            expired = time.time() - stats.lib.STATE_TTL_SECONDS - 1
            os.utime(old, (expired, expired))
            self.assertEqual(stats.verify_compliance(d), (1, 0))


class CodexWorkerSummaryTest(unittest.TestCase):
    def test_aggregates_routes_statuses_usage_and_duration(self):
        with tempfile.TemporaryDirectory() as d:
            records = (
                {
                    "created_at": "2026-09-03T10:00:00+09:00",
                    "invocation_status": "completed",
                    "worker_status": "completed",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                    "duration_seconds": 10,
                    "usage": {"input_tokens": 100, "cached_input_tokens": 80,
                              "output_tokens": 20, "reasoning_output_tokens": 5},
                },
                {
                    "created_at": "2026-09-03T10:10:00+09:00",
                    "invocation_status": "failed",
                    "worker_status": "unknown",
                    "model": "gpt-5.6-sol",
                    "effort": "xhigh",
                    "duration_seconds": 30,
                    "usage": {"input_tokens": 50, "output_tokens": 10},
                },
            )
            for index, record in enumerate(records):
                with open(os.path.join(d, "%d.json" % index), "w") as f:
                    json.dump(record, f)
            summary = stats.codex_worker_summary(d, since_ts=0)
            self.assertEqual(summary["runs"], 2)
            self.assertEqual(summary["statuses"]["completed"], 1)
            self.assertEqual(summary["statuses"]["failed"], 1)
            self.assertEqual(summary["usage"]["input_tokens"], 150)
            self.assertEqual(summary["duration_seconds"], 40)


class ClaudeUsageSummaryTest(unittest.TestCase):
    @staticmethod
    def request(message_id="msg-1", timestamp="2026-09-07T10:00:00+09:00", output_tokens=30):
        return {
            "timestamp": timestamp,
            "uuid": "row-1",
            "message": {
                "id": message_id,
                "model": "test-model",
                "usage": {"input_tokens": 100, "output_tokens": output_tokens},
            },
        }

    @staticmethod
    def write_rows(path, rows):
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def test_deduplicates_streamed_messages_and_groups_effort(self):
        with tempfile.TemporaryDirectory() as d:
            project = os.path.join(d, "project")
            os.mkdir(project)
            path = os.path.join(project, "session.jsonl")
            base = {
                "timestamp": "2026-09-07T10:00:00+09:00",
                "uuid": "row-1",
                "effort": "medium",
                "isSidechain": False,
                "message": {
                    "id": "msg-1",
                    "model": "claude-fable-5-1",
                    "usage": {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 10,
                        "cache_read_input_tokens": 20,
                        "output_tokens": 30,
                        "output_tokens_details": {"thinking_tokens": 12},
                    },
                },
            }
            sidechain = json.loads(json.dumps(base))
            sidechain["uuid"] = "row-2"
            sidechain["message"]["id"] = "msg-2"
            sidechain["isSidechain"] = True
            with open(path, "w") as f:
                f.write(json.dumps(base) + "\n")
                f.write(json.dumps(base) + "\n")  # streaming duplicate
                f.write(json.dumps(sidechain) + "\n")
            summary = stats.claude_usage_summary(d, since_ts=0)
            self.assertEqual(summary["requests"], 2)
            self.assertEqual(summary["sessions"], 1)
            self.assertEqual(summary["sidechain_requests"], 1)
            self.assertEqual(summary["routes"]["claude-fable-5-1/medium"], 2)
            self.assertEqual(summary["usage"]["output_tokens"], 60)
            self.assertEqual(summary["usage"]["thinking_tokens"], 24)

    def test_old_files_are_skipped_before_opening(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, "old.jsonl")
            fresh = os.path.join(d, "fresh.jsonl")
            self.write_rows(old, [self.request("old")])
            self.write_rows(fresh, [self.request("fresh")])
            cutoff = datetime.datetime.fromisoformat("2026-09-01T00:00:00+09:00").timestamp()
            os.utime(old, (cutoff - 10, cutoff - 10))
            os.utime(fresh, (cutoff + 10, cutoff + 10))
            with patch("builtins.open", wraps=open) as opened:
                summary = stats.claude_usage_summary(d, since_ts=cutoff)
            self.assertEqual([call.args[0] for call in opened.call_args_list], [fresh])
            self.assertEqual(summary["requests"], 1)

    def test_cross_file_duplicates_keep_latest_timestamp_regardless_of_file_order(self):
        with tempfile.TemporaryDirectory() as d:
            older = os.path.join(d, "older.jsonl")
            newer = os.path.join(d, "newer.jsonl")
            self.write_rows(older, [self.request(output_tokens=10)])
            self.write_rows(newer, [self.request(timestamp="2026-09-07T10:01:00+09:00", output_tokens=30)])
            for paths in ([newer, older], [older, newer]):
                with self.subTest(paths=paths), patch("stats.glob.glob", return_value=paths):
                    summary = stats.claude_usage_summary(d)
                self.assertEqual(summary["requests"], 1)
                self.assertEqual(summary["usage"]["input_tokens"], 100)
                self.assertEqual(summary["usage"]["output_tokens"], 30)

    def test_equal_timestamp_streaming_fragments_keep_larger_output(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "session.jsonl")
            self.write_rows(path, [self.request(output_tokens=30), self.request(output_tokens=10)])
            summary = stats.claude_usage_summary(d)
            self.assertEqual(summary["requests"], 1)
            self.assertEqual(summary["usage"]["output_tokens"], 30)

    def test_synthetic_and_malformed_rows_do_not_count_as_requests(self):
        with tempfile.TemporaryDirectory() as d:
            synthetic_model = self.request("synthetic-model")
            synthetic_model["message"]["model"] = "<synthetic>"
            synthetic_flag = self.request("synthetic-flag")
            synthetic_flag["isSynthetic"] = True
            malformed_message = self.request("malformed")
            malformed_message["message"] = "not a message"
            self.write_rows(os.path.join(d, "session.jsonl"), [
                [], None, {"timestamp": 123}, malformed_message,
                synthetic_model, synthetic_flag, self.request(),
            ])
            summary = stats.claude_usage_summary(d)
            self.assertEqual(summary["requests"], 1)
            self.assertEqual(summary["usage"]["input_tokens"], 100)

    def test_uuid_fallback_is_scoped_to_file(self):
        with tempfile.TemporaryDirectory() as d:
            row = self.request()
            del row["message"]["id"]
            for name in ("a.jsonl", "b.jsonl"):
                self.write_rows(os.path.join(d, name), [row, row])
            summary = stats.claude_usage_summary(d)
            self.assertEqual(summary["requests"], 2)


class CollectReportTest(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(stats.lib, "EVENTS_PATH", os.path.join(directory, "missing.jsonl")))
        self.stack.enter_context(patch("stats.verify_compliance", return_value=(2, 1)))
        self.stack.enter_context(patch("stats.handoff_summary", return_value={"routes": {}}))
        self.stack.enter_context(patch("stats.codex_worker_summary", return_value={"runs": 0, "usage": stats.Counter(input_tokens=100)}))
        self.claude = self.stack.enter_context(patch("stats.claude_usage_summary", return_value={"requests": 1}))

    def test_default_report_never_reads_claude_transcripts_and_is_json_serializable(self):
        report = stats.collect_report(7)
        self.claude.assert_not_called()
        self.assertIsNone(report["claude"])
        self.assertFalse(report["claude_included"])
        self.assertEqual(json.loads(json.dumps(report))["codex_workers"]["usage"]["input_tokens"], 100)
        self.assertEqual(report["verification"]["verified"], 1)

    def test_explicit_claude_report_shares_the_reporting_cutoff(self):
        with patch("stats.time.time", return_value=1000000):
            report = stats.collect_report(2, include_claude=True)
        self.claude.assert_called_once_with(since_ts=1000000 - 2 * 86400)
        self.assertTrue(report["claude_included"])
        self.assertEqual(report["claude"]["requests"], 1)

    def test_cli_json_preserves_days_argument_and_requires_claude_opt_in(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            stats.main(["7", "--json"])
        self.claude.assert_not_called()
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["days"], 7)
        self.assertFalse(report["claude_included"])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            stats.main(["7", "--json", "--include-claude"])
        self.assertTrue(json.loads(stdout.getvalue())["claude_included"])
        self.claude.assert_called_once()

    def test_nonpositive_and_nonfinite_days_are_rejected(self):
        for days in (0, -1, float("nan"), float("inf")):
            with self.subTest(days=days), self.assertRaises(ValueError):
                stats.collect_report(days)
        self.claude.assert_not_called()


if __name__ == "__main__":
    unittest.main()
