import unittest

import stats

LINES = [
    '{"t":"2026-07-06T10:00:00","kind":"plan-deny","session":"aaaa1111","detail":"/w/src/a.ts"}',
    '{"t":"2026-07-06T10:01:00","kind":"plan-deny","session":"aaaa1111","detail":"/w/src/a.ts"}',
    '{"t":"2026-07-06T10:02:00","kind":"quick-bypass","session":"bbbb2222","detail":""}',
    '{"t":"2026-07-06T10:03:00","kind":"map-inject","session":"bbbb2222","detail":"todari"}',
    'not-json-garbage',
    '{"t":"2020-01-01T00:00:00","kind":"plan-deny","session":"old00000","detail":"/old.ts"}',
]


class SummarizeTest(unittest.TestCase):
    def test_counts_kinds_and_sessions(self):
        s = stats.summarize(LINES, since_ts=0)
        self.assertEqual(s["kinds"]["plan-deny"], 3)
        self.assertEqual(s["kinds"]["quick-bypass"], 1)
        self.assertEqual(s["sessions"], 3)

    def test_since_filter_excludes_old(self):
        import time
        cutoff = time.mktime(time.strptime("2026-01-01T00:00:00", "%Y-%m-%dT%H:%M:%S"))
        s = stats.summarize(LINES, since_ts=cutoff)
        self.assertEqual(s["kinds"]["plan-deny"], 2)
        self.assertEqual(s["sessions"], 2)

    def test_top_denied_details(self):
        s = stats.summarize(LINES, since_ts=0)
        self.assertEqual(s["top_denied"][0], ("/w/src/a.ts", 2))

    def test_garbage_lines_ignored(self):
        s = stats.summarize(["broken", "{}"], since_ts=0)
        self.assertEqual(s["sessions"], 0)


if __name__ == "__main__":
    unittest.main()
