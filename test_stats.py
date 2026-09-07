import json
import os
import tempfile
import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
