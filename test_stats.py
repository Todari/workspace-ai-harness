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


if __name__ == "__main__":
    unittest.main()
