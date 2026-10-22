import json
import os
import tempfile
import unittest

import context_watch as cw


def _usage_line(total, kind="assistant"):
    return json.dumps({"type": kind, "message": {"usage": {
        "input_tokens": 10, "cache_read_input_tokens": total - 10,
        "cache_creation_input_tokens": 0}}})


class ContextTokens(unittest.TestCase):
    def test_reads_last_assistant_usage_only(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(_usage_line(800_000) + "\n")
            f.write(_usage_line(300_000) + "\n")
            f.write('{"type":"user","message":{"content":"usage"}}\n')
            f.write("not json\n")
            path = f.name
        try:
            self.assertEqual(cw.last_context_tokens(path), 300_000)
        finally:
            os.remove(path)

    def test_missing_transcript_is_zero(self):
        self.assertEqual(cw.last_context_tokens("/nonexistent/x.jsonl"), 0)
        self.assertEqual(cw.last_context_tokens(""), 0)

    def test_advice_thresholds(self):
        self.assertEqual(cw.context_advice(100_000), "")
        self.assertIn("/compact", cw.context_advice(260_000))
        self.assertIn("새 세션", cw.context_advice(600_000))


class OutputAdvice(unittest.TestCase):
    def test_small_or_unlisted_tool_is_silent(self):
        self.assertEqual(cw.output_advice("Bash", {"stdout": "ok"}), "")
        self.assertEqual(cw.output_advice("Edit", "x" * 20_000), "")
        self.assertEqual(cw.output_advice("Read", "x" * 20_000), "")

    def test_large_output_warns(self):
        self.assertIn("head/tail/grep", cw.output_advice("Bash", {"stdout": "x" * 9_000}))
        self.assertIn("head/tail/grep", cw.output_advice("Grep", "x" * 9_000))
        self.assertIn("head/tail/grep", cw.output_advice("WebFetch", [{"text": "x" * 9_000}]))


if __name__ == "__main__":
    unittest.main()
