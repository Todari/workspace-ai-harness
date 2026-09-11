import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import harness_lib as lib
import repo_map


def make_fixture_repo(base):
    os.makedirs(os.path.join(base, "apps", "client", "src"))
    with open(os.path.join(base, "package.json"), "w") as f:
        json.dump({
            "scripts": {"build": "turbo build", "test": "turbo test"},
            "devDependencies": {"typescript": "^5.0.0"},
        }, f)
    with open(os.path.join(base, "apps", "client", "package.json"), "w") as f:
        json.dump({
            "scripts": {"dev": "vite dev", "type-check": "tsc --noEmit"},
            "dependencies": {"react": "^18.0.0"},
        }, f)
    with open(os.path.join(base, "pnpm-lock.yaml"), "w") as f:
        f.write("lockfileVersion: 9\n")
    subprocess.run(["git", "-C", base, "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", base, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "initial commit"],
        check=True)


class BuildMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        make_fixture_repo(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_map_contains_stack_scripts_tree_and_git_status(self):
        text = repo_map.build_map(self.tmp.name)
        self.assertIn("패키지 매니저: pnpm", text)
        self.assertIn("typescript@^5.0.0", text)
        self.assertIn("`build`", text)          # 루트 스크립트
        self.assertIn("`type-check`", text)     # 워크스페이스 스크립트
        self.assertIn("apps/client", text)
        self.assertIn("현재 git 상태", text)
        self.assertIn("## ", text)

    def test_map_points_to_repo_claude_md_and_docs(self):
        with open(os.path.join(self.tmp.name, "CLAUDE.md"), "w") as f:
            f.write("# rules\n")
        os.makedirs(os.path.join(self.tmp.name, "docs"))
        text = repo_map.build_map(self.tmp.name)
        self.assertIn("레포 CLAUDE.md 있음", text)
        self.assertIn("docs/ 디렉토리 있음", text)

    def test_map_points_to_repo_agents_md(self):
        with open(os.path.join(self.tmp.name, "AGENTS.md"), "w") as f:
            f.write("# shared rules\n")
        self.assertIn("레포 AGENTS.md 있음", repo_map.build_map(self.tmp.name))

    def test_map_warns_that_both_instruction_files_are_not_auto_merged(self):
        for name in ("AGENTS.md", "CLAUDE.md"):
            with open(os.path.join(self.tmp.name, name), "w") as f:
                f.write("# rules\n")
        self.assertIn("자동 병합하지 않음", repo_map.build_map(self.tmp.name))

    def test_map_omits_pointers_when_absent(self):
        text = repo_map.build_map(self.tmp.name)
        self.assertNotIn("레포 CLAUDE.md 있음", text)


class DeepContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = repo_map.CONTEXT_DIR
        repo_map.CONTEXT_DIR = self.tmp.name

    def tearDown(self):
        repo_map.CONTEXT_DIR = self.orig
        self.tmp.cleanup()

    def test_returns_content_when_present(self):
        with open(os.path.join(self.tmp.name, "todari.md"), "w") as f:
            f.write("### 심층 컨텍스트\n내용\n")
        self.assertIn("심층 컨텍스트",
                      repo_map.deep_context(os.path.join(lib.WORKSPACE_ROOT, "projects", "todari")))

    def test_returns_none_when_absent(self):
        self.assertIsNone(repo_map.deep_context(os.path.join(lib.WORKSPACE_ROOT, "projects", "nope")))

    def test_summary_keeps_identity_safety_and_completion_but_not_full_body(self):
        content = """### 심층 컨텍스트: demo

demo 서비스 한 줄 설명.

**아키텍처**
- 세부 구현 A
- 세부 구현 B
- 세부 구현 C

**주의사항**
- main 직접 push 금지 — PR만 허용.

**완료 기준**
`pnpm lint && pnpm test`
"""
        summary = repo_map.summarize_deep_context(
            os.path.join(lib.WORKSPACE_ROOT, "projects", "demo"), content)
        self.assertIn("demo 서비스 한 줄 설명", summary)
        self.assertIn("main 직접 push 금지", summary)
        self.assertIn("pnpm lint && pnpm test", summary)
        self.assertNotIn("세부 구현 A", summary)
        self.assertIn(os.path.join(self.tmp.name, "demo.md"), summary)

    def test_summary_is_bounded(self):
        content = "서비스 설명\n" + "\n".join(
            "- main 배포 주의 %02d %s" % (i, "x" * 250) for i in range(40))
        summary = repo_map.summarize_deep_context(
            os.path.join(lib.WORKSPACE_ROOT, "projects", "demo"), content)
        self.assertLessEqual(summary.count("\n"), repo_map.MAX_DEEP_CONTEXT_LINES + 3)
        self.assertLess(len(summary), repo_map.MAX_DEEP_CONTEXT_CHARS + 500)


class StalenessTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.TemporaryDirectory()
        self.docs = tempfile.TemporaryDirectory()
        self.doc = os.path.join(self.docs.name, "ctx.md")
        with open(self.doc, "w") as f:
            f.write("### 심층 컨텍스트\n")
        past = os.path.getmtime(self.doc) - 3600
        os.utime(self.doc, (past, past))  # 문서를 1시간 전 작성으로 설정
        subprocess.run(["git", "-C", self.repo.name, "init", "-q"], check=True)
        for i in range(3):
            subprocess.run(
                ["git", "-C", self.repo.name, "-c", "user.email=t@t",
                 "-c", "user.name=t", "commit", "--allow-empty", "-q",
                 "-m", "c%d" % i], check=True)
        self.orig = repo_map.STALE_COMMITS_THRESHOLD

    def tearDown(self):
        repo_map.STALE_COMMITS_THRESHOLD = self.orig
        self.repo.cleanup()
        self.docs.cleanup()

    def test_warns_when_commits_exceed_threshold(self):
        repo_map.STALE_COMMITS_THRESHOLD = 2
        warning = repo_map.staleness_warning(self.repo.name, self.doc)
        self.assertIsNotNone(warning)
        self.assertIn("3개", warning)

    def test_silent_under_threshold(self):
        repo_map.STALE_COMMITS_THRESHOLD = 10
        self.assertIsNone(repo_map.staleness_warning(self.repo.name, self.doc))

    def test_non_git_root_is_silent(self):
        repo_map.STALE_COMMITS_THRESHOLD = 1
        self.assertIsNone(repo_map.staleness_warning(self.docs.name, self.doc))

    def test_missing_doc_is_silent(self):
        repo_map.STALE_COMMITS_THRESHOLD = 1
        self.assertIsNone(repo_map.staleness_warning(
            self.repo.name, self.docs.name + "/nope.md"))


class TreeCapTest(unittest.TestCase):
    def test_caps_children_per_dir(self):
        with tempfile.TemporaryDirectory() as base:
            for i in range(12):
                os.makedirs(os.path.join(base, "junk", "theme%02d" % i))
            os.makedirs(os.path.join(base, "src", "components"))
            lines = repo_map.tree_lines(base)
            junk_children = [l for l in lines if "junk/theme" in l]
            self.assertLessEqual(len(junk_children), repo_map.MAX_CHILDREN_PER_DIR)
            self.assertIn("- junk/… (+4 dirs)", lines)
            self.assertIn("- src/components/", lines)


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = lib.MAP_CACHE_DIR
        lib.MAP_CACHE_DIR = os.path.join(self.tmp.name, "maps")

    def tearDown(self):
        lib.MAP_CACHE_DIR = self.orig
        self.tmp.cleanup()

    def test_round_trip(self):
        repo_map.save_cached_map("/repo/a", "head1", "MAP")
        self.assertEqual(repo_map.load_cached_map("/repo/a", "head1"), "MAP")

    def test_fingerprint_mismatch_invalidates(self):
        repo_map.save_cached_map("/repo/a", "head1", "MAP")
        self.assertIsNone(repo_map.load_cached_map("/repo/a", "head2"))

    def test_missing_cache_is_none(self):
        self.assertIsNone(repo_map.load_cached_map("/repo/never", "h"))


class FingerprintTest(unittest.TestCase):
    def test_upstream_only_change_invalidates_cached_git_status(self):
        with tempfile.TemporaryDirectory() as d:
            make_fixture_repo(d)
            branch = repo_map.git(d, "branch", "--show-current")
            head = repo_map.git(d, "rev-parse", "HEAD")
            subprocess.run(["git", "-C", d, "branch", "upstream"], check=True)
            subprocess.run(["git", "-C", d, "branch", "--set-upstream-to=upstream"],
                           check=True, capture_output=True)
            before = repo_map.repo_fingerprint(d)
            subprocess.run(["git", "-C", d, "switch", "-q", "upstream"], check=True)
            subprocess.run(["git", "-C", d, "-c", "user.email=t@t", "-c", "user.name=t",
                            "commit", "--allow-empty", "-q", "-m", "upstream advance"], check=True)
            subprocess.run(["git", "-C", d, "switch", "-q", branch], check=True)
            self.assertEqual(repo_map.git(d, "rev-parse", "HEAD"), head)
            self.assertIn("behind 1", repo_map.build_map(d))
            self.assertNotEqual(before, repo_map.repo_fingerprint(d))

    def test_renderer_change_invalidates_fingerprint_without_repo_change(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as renderer_dir:
            make_fixture_repo(d)
            renderer = os.path.join(renderer_dir, "renderer.py")
            with open(renderer, "w") as f:
                f.write("old renderer")
            with patch.object(repo_map, "__file__", renderer):
                before = repo_map.repo_fingerprint(d)
                with open(renderer, "w") as f:
                    f.write("new renderer")
                self.assertNotEqual(before, repo_map.repo_fingerprint(d))

    def test_dirty_metadata_invalidates_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            make_fixture_repo(d)
            before = repo_map.repo_fingerprint(d)
            with open(os.path.join(d, "package.json"), "a") as f:
                f.write("\n")
            self.assertNotEqual(before, repo_map.repo_fingerprint(d))

    def test_dirty_source_outside_metadata_dirs_invalidates_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            make_fixture_repo(d)
            source = os.path.join(d, "src.py")
            with open(source, "w") as f:
                f.write("before = True\n")
            subprocess.run(["git", "-C", d, "add", "src.py"], check=True)
            subprocess.run(
                ["git", "-C", d, "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-q", "-m", "add source"], check=True)
            before = repo_map.repo_fingerprint(d)
            with open(source, "a") as f:
                f.write("after = True\n")
            self.assertNotEqual(before, repo_map.repo_fingerprint(d))


class RepoRootTest(unittest.TestCase):
    def test_non_git_falls_back_to_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(repo_map.repo_root(d), d)
            self.assertEqual(repo_map.git_root(d), "")


class LeanModeTest(unittest.TestCase):
    """지침 파일이 있는 레포는 구조를 그 문서가 담당하므로 트리를 1 depth로 줄인다."""

    def test_instruction_file_shrinks_tree(self):
        with tempfile.TemporaryDirectory() as d:
            make_fixture_repo(d)
            full = repo_map.build_map(d)
            with open(os.path.join(d, "AGENTS.md"), "w") as f:
                f.write("# rules\n")
            lean = repo_map.build_map(d)
        self.assertIn("- apps/client/", full)
        self.assertNotIn("- apps/client/", lean)
        self.assertIn("- apps/", lean)
        self.assertIn("(1 depth)", lean)
        self.assertIn("(2 depth)", full)


class GroupIndexTest(unittest.TestCase):
    """git 레포가 아닌 그룹 디렉토리는 하위 레포 요약을 준다."""

    def test_group_rows_list_sub_repos_only(self):
        with tempfile.TemporaryDirectory() as d:
            make_fixture_repo(os.path.join(d, "alpha"))
            os.makedirs(os.path.join(d, "plain-dir"))
            rows = repo_map.group_rows(d)
            text = repo_map.build_group_map(d)
        self.assertEqual(len(rows), 1)
        self.assertIn("alpha/", rows[0])
        self.assertIn("최근 커밋", rows[0])
        self.assertNotIn("plain-dir", text)
        self.assertIn("git 레포 아님", text)

    def test_empty_group_is_blank(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(repo_map.build_group_map(d), "")

    def test_group_rows_counts_untracked_changes(self):
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "alpha")
            make_fixture_repo(repo)
            subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
            subprocess.run(
                ["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-q", "-m", "fixture"], check=True)
            with open(os.path.join(repo, "new.txt"), "w") as f:
                f.write("new\n")
            rows = repo_map.group_rows(d)
        self.assertIn("변경 1개", rows[0])


class ShouldInjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        self.orig = lib.WORKSPACE_ROOT
        lib.WORKSPACE_ROOT = self.ws

    def tearDown(self):
        lib.WORKSPACE_ROOT = self.orig
        self.tmp.cleanup()

    def test_workspace_root_itself_is_skipped(self):
        self.assertFalse(repo_map.should_inject(self.ws))

    def test_sub_repo_is_injected(self):
        sub = os.path.join(self.ws, "projects", "todari")
        os.makedirs(sub)
        self.assertTrue(repo_map.should_inject(sub))


if __name__ == "__main__":
    unittest.main()
