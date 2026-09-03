import json
import os
import tempfile
import time
import unittest

import harness_lib as lib

ROOT = lib.WORKSPACE_ROOT


class InWorkspaceTest(unittest.TestCase):
    def test_inside(self):
        self.assertTrue(lib.in_workspace(os.path.join(ROOT, "projects", "demo")))

    def test_root_itself(self):
        self.assertTrue(lib.in_workspace(ROOT))

    def test_outside(self):
        self.assertFalse(lib.in_workspace(os.path.join(os.path.dirname(ROOT), "other")))

    def test_prefix_trick(self):
        self.assertFalse(lib.in_workspace(ROOT + "-evil"))

    def test_empty(self):
        self.assertFalse(lib.in_workspace(""))


class PruneOldFilesTest(unittest.TestCase):
    def test_removes_old_keeps_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, "old")
            fresh = os.path.join(d, "fresh")
            for path in (old, fresh):
                open(path, "w").close()
            stale = time.time() - lib.STATE_TTL_SECONDS - 60
            os.utime(old, (stale, stale))
            lib.prune_old_files(d, lib.STATE_TTL_SECONDS)
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(fresh))

    def test_missing_dir_is_noop(self):
        lib.prune_old_files("/nonexistent/dir", 10)  # 예외 없이 통과해야 함


class HookOutputTest(unittest.TestCase):
    def test_shape(self):
        out = json.loads(lib.hook_output("SessionStart", "안녕"))
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], "안녕")


class LogCapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = lib.LOG_PATH
        lib.LOG_PATH = self.tmp.name + "/harness.log"

    def tearDown(self):
        lib.LOG_PATH = self.orig
        self.tmp.cleanup()

    def test_truncates_when_over_cap(self):
        with open(lib.LOG_PATH, "w") as f:
            for i in range(20000):
                f.write("line %d padding-padding-padding-padding-padding\n" % i)
        self.assertGreater(os.path.getsize(lib.LOG_PATH), lib.MAX_LOG_BYTES)
        lib.log("after-cap")
        size = os.path.getsize(lib.LOG_PATH)
        self.assertLess(size, lib.MAX_LOG_BYTES)
        with open(lib.LOG_PATH) as f:
            content = f.read()
        self.assertIn("after-cap", content)
        self.assertIn("line 19999", content)   # 최근 로그는 보존
        self.assertNotIn("line 0 ", content)   # 오래된 로그는 버림


class EventLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = lib.EVENTS_PATH
        lib.EVENTS_PATH = self.tmp.name + "/events.jsonl"

    def tearDown(self):
        lib.EVENTS_PATH = self.orig
        self.tmp.cleanup()

    def test_writes_parseable_jsonl(self):
        lib.event("map-inject", "abcdef123456", "todari:1200")
        lib.event("verify-block", "abcdef123456")
        with open(lib.EVENTS_PATH) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["kind"], "map-inject")
        self.assertEqual(first["session"], "abcdef12")  # 8자로 절단
        self.assertEqual(first["detail"], "todari:1200")
        self.assertIn("t", first)

    def test_truncates_when_over_cap(self):
        with open(lib.EVENTS_PATH, "w") as f:
            for i in range(40000):
                f.write('{"kind":"old","i":%d,"pad":"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}\n' % i)
        self.assertGreater(os.path.getsize(lib.EVENTS_PATH), lib.MAX_EVENTS_BYTES)
        lib.event("new-kind", "s")
        self.assertLess(os.path.getsize(lib.EVENTS_PATH), lib.MAX_EVENTS_BYTES)
        with open(lib.EVENTS_PATH) as f:
            content = f.read()
        self.assertIn("new-kind", content)
        self.assertNotIn('"i":0,', content)


class FailOpenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = lib.LOG_PATH
        lib.LOG_PATH = self.tmp.name + "/harness.log"  # 실제 로그 오염 방지

    def tearDown(self):
        lib.LOG_PATH = self.orig
        self.tmp.cleanup()

    def test_exits_zero_on_error(self):
        def boom():
            raise RuntimeError("boom")
        with self.assertRaises(SystemExit) as ctx:
            lib.run_fail_open(boom)
        self.assertEqual(ctx.exception.code, 0)

    def test_exits_zero_on_success(self):
        with self.assertRaises(SystemExit) as ctx:
            lib.run_fail_open(lambda: None)
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
