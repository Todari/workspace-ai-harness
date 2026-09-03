import json
import os
import subprocess
import tempfile
import unittest

import harness_lib as lib
import repos

SAMPLE = [
    {"name": "todari", "path": "projects/todari", "rule": "main 직접 푸시 가능",
     "vault_note": "프로젝트/todari.md"},
    {"name": "포크레터", "path": "linkive/forcletter", "rule": "dev 통합",
     "vault_note": "포크레터/포크레터.md"},
    {"name": "이정표", "path": "projects/basetie", "aliases": ["jeongpyo"],
     "vault_note": "이정표/이정표.md"},
    {"name": "jujinmo", "path": "projects/jujinmo"},
]


class RegistryTest(unittest.TestCase):
    def test_load_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "repos.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"repos": SAMPLE + [{"name": "broken"}]}, f)
            loaded = repos.load(path)
        self.assertEqual([r["name"] for r in loaded], ["todari", "포크레터", "이정표", "jujinmo"])

    def test_missing_file_is_empty(self):
        self.assertEqual(repos.load("/nonexistent/repos.json"), [])

    def test_vault_note_direct_alias_and_worktree(self):
        self.assertEqual(repos.vault_note_for("todari", SAMPLE), "프로젝트/todari.md")
        self.assertEqual(repos.vault_note_for("jeongpyo", SAMPLE), "이정표/이정표.md")
        self.assertEqual(repos.vault_note_for("forcletter-seo", SAMPLE), "포크레터/포크레터.md")
        self.assertEqual(repos.vault_note_for("unknown", SAMPLE), "")
        self.assertEqual(repos.vault_note_for("jujinmo", SAMPLE), "")

    def test_markdown_table_lists_only_repos_with_rules(self):
        table = repos.markdown_table(SAMPLE)
        self.assertIn("| todari | `projects/todari` | main 직접 푸시 가능 |", table)
        self.assertIn("포크레터", table)
        self.assertNotIn("jujinmo", table)
        self.assertEqual(repos.markdown_table([]), "")


class ActiveTest(unittest.TestCase):
    def test_active_counts_recent_commits_and_marks_unregistered(self):
        with tempfile.TemporaryDirectory() as d:
            ws = os.path.realpath(d)
            repo = os.path.join(ws, "projects", "fresh")
            os.makedirs(repo)
            subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
            subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t",
                            "commit", "--allow-empty", "-q", "-m", "c"], check=True)
            os.makedirs(os.path.join(ws, "projects", "plain"))
            orig_ws, orig_reg = lib.WORKSPACE_ROOT, repos.REGISTRY
            lib.WORKSPACE_ROOT, repos.REGISTRY = ws, "/nonexistent/repos.json"
            try:
                rows = repos.active(7)
            finally:
                lib.WORKSPACE_ROOT, repos.REGISTRY = orig_ws, orig_reg
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], "projects/fresh")
        self.assertEqual(rows[0]["commits"], 1)
        self.assertFalse(rows[0]["registered"])


if __name__ == "__main__":
    unittest.main()
