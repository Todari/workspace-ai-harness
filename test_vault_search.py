"""Search must distinguish an empty vault from unreadable or partial results."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import vault_search as search


class SearchErrorsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(search, "VAULT", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_search(self, *args):
        with mock.patch("sys.argv", ["vault_search.py", *args]), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = search.main()
        return code, out.getvalue(), err.getvalue()

    def test_empty_readable_vault_is_success(self):
        code, out, err = self.run_search("needle")
        self.assertEqual(code, 0)
        self.assertIn("0건", out)
        self.assertEqual(err, "")

    def test_unreadable_file_is_not_reported_as_zero_matches(self):
        Path(self.tmp.name, "note.md").touch()
        with mock.patch("builtins.open", side_effect=PermissionError("private detail")):
            code, out, err = self.run_search("needle")
        self.assertEqual(code, 1)
        self.assertNotIn("0건", out)
        self.assertIn("읽기 실패 1건", err)
        self.assertNotIn("private detail", err)

    def test_walk_error_preserves_partial_results(self):
        note = Path(self.tmp.name, "note.md")
        note.write_text("---\ntype: TIL\n---\nneedle", encoding="utf-8")

        def partial_walk(root, onerror):
            yield root, [], [note.name]
            onerror(PermissionError("private path"))

        for args in (("needle",), ("--list", "TIL")):
            with self.subTest(args=args), mock.patch.object(search.os, "walk", partial_walk):
                code, out, err = self.run_search(*args)
            self.assertEqual(code, 1)
            self.assertIn("note.md", out)
            self.assertIn("부분 결과", out)
            self.assertIn("읽기 실패 1건", err)

    def test_root_walk_error_is_failure(self):
        def failed_walk(root, onerror):
            onerror(PermissionError("private path"))
            return iter(())

        with mock.patch.object(search.os, "walk", failed_walk):
            code, out, err = self.run_search("needle")
        self.assertEqual(code, 1)
        self.assertNotIn("0건", out)
        self.assertIn("검색 불완전", err)

    def test_unreadable_file_in_list_mode_is_failure(self):
        Path(self.tmp.name, "note.md").touch()
        with mock.patch("builtins.open", side_effect=OSError("private detail")):
            code, out, err = self.run_search("--list", "TIL")
        self.assertEqual(code, 1)
        self.assertIn("부분 결과", out)
        self.assertIn("읽기 실패 1건", err)

    def test_usage_does_not_require_accessible_vault(self):
        with mock.patch.object(search, "VAULT", os.path.join(self.tmp.name, "missing")):
            code, out, err = self.run_search()
        self.assertEqual(code, 2)
        self.assertIn("usage", out)


if __name__ == "__main__":
    unittest.main()
