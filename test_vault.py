#!/usr/bin/env python3
"""볼트 연동(검색 CLI · 브리지 훅 · 서브에이전트 훅) 회귀 테스트."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import harness_lib as lib

HERE = os.path.dirname(os.path.abspath(__file__))
SEARCH = os.path.join(HERE, "vault_search.py")
BRIDGE = os.path.join(HERE, "obsidian_bridge.py")
SUBAGENT = os.path.join(HERE, "vault_subagent.py")
VAULT = lib.VAULT_ROOT
HAS_VAULT = os.path.isdir(VAULT)


def run(script, stdin="", args=(), env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run([sys.executable, script] + list(args),
                          input=stdin, capture_output=True, text=True,
                          timeout=60, env=e)


class VaultSearchTest(unittest.TestCase):
    def test_no_args_prints_usage(self):
        p = run(SEARCH)
        self.assertEqual(p.returncode, 2)
        self.assertIn("usage", p.stdout.lower())

    @unittest.skipUnless(HAS_VAULT, "볼트 접근 불가")
    def test_korean_query_matches(self):
        """iCloud NFD 파일명 함정 — grep -r이 실패하는 케이스가 여기선 잡혀야 한다."""
        p = run(SEARCH, args=["배낭"])
        self.assertEqual(p.returncode, 0)
        self.assertIn("Knapsack", p.stdout)

    @unittest.skipUnless(HAS_VAULT, "볼트 접근 불가")
    def test_missing_query_reports_zero_quietly(self):
        p = run(SEARCH, args=["존재하지않는검색어zzqq"])
        self.assertEqual(p.returncode, 0)
        self.assertIn("0건", p.stdout)

    @unittest.skipUnless(HAS_VAULT, "볼트 접근 불가")
    def test_type_filter_restricts_results(self):
        p = run(SEARCH, args=["React", "-t", "프로젝트"])
        self.assertEqual(p.returncode, 0)
        for line in p.stdout.splitlines():
            if line.startswith("## "):
                self.assertIn("[type: 프로젝트]", line)

    @unittest.skipUnless(HAS_VAULT, "볼트 접근 불가")
    def test_list_mode(self):
        p = run(SEARCH, args=["--list", "프로젝트"])
        self.assertIn("프로젝트/todari.md", p.stdout)


class BridgeHookTest(unittest.TestCase):
    def test_outside_workspace_is_silent(self):
        p = run(BRIDGE, stdin=json.dumps({"cwd": "/tmp"}))
        self.assertEqual(p.stdout.strip(), "")

    def test_unmapped_repo_is_silent(self):
        p = run(BRIDGE, stdin=json.dumps(
            {"cwd": os.path.join(lib.WORKSPACE_ROOT, "go", "src", "example")}))
        self.assertEqual(p.stdout.strip(), "")

    @unittest.skipUnless(HAS_VAULT, "볼트 접근 불가")
    def test_mapped_repo_injects_conditional_update_instruction(self):
        p = run(BRIDGE, stdin=json.dumps(
            {"cwd": os.path.join(lib.WORKSPACE_ROOT, "projects", "2024-haeng-dong")}))
        out = json.loads(p.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("haengdong.md", ctx)
        self.assertIn("다음 할 일", ctx)
        self.assertIn("실제로 바뀐 경우에만", ctx)
        self.assertIn("일반 코드 수정·검증·질문만으로는 쓰지 않는다", ctx)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_malformed_input_fails_open(self):
        p = run(BRIDGE, stdin="not json")
        self.assertEqual(p.returncode, 0)


class KnowledgeIndexTest(unittest.TestCase):
    """전용 폴더를 가진 프로젝트만 지식 인덱스를 받는다."""

    def setUp(self):
        sys.path.insert(0, HERE)
        import obsidian_bridge
        self.b = obsidian_bridge

    def test_one_liner_ignores_placeholder(self):
        self.assertEqual(
            self.b.one_liner("> **한 줄 정의:** (무엇을, 누구에게, 왜) — 채워넣기"), "")
        self.assertEqual(
            self.b.one_liner("> **한 줄 정의:** 취준생을 위한 로드맵 앱"),
            "취준생을 위한 로드맵 앱")

    def test_task_preview_caps_injected_backlog(self):
        tasks = [f"- [ ] task {i}" for i in range(8)]
        preview = self.b.task_preview(tasks)
        self.assertEqual(len(preview), 4)
        self.assertIn("외 5건", preview[-1])

    def test_shared_project_folder_has_no_index(self):
        """프로젝트/ 아래 단일 노트는 지식베이스가 아니므로 인덱스를 만들지 않는다."""
        note = os.path.join(VAULT, "프로젝트", "todari.md")
        self.assertEqual(self.b.knowledge_index(note), "")

    def test_index_lists_only_nonempty_folders(self):
        with tempfile.TemporaryDirectory() as d:
            hub = os.path.join(d, "허브.md")
            with open(hub, "w") as f:
                f.write("x")
            os.makedirs(os.path.join(d, "기술"))
            os.makedirs(os.path.join(d, "빈폴더"))
            with open(os.path.join(d, "기술", "스택 선정.md"), "w") as f:
                f.write("x")
            with open(os.path.join(d, "_용어집.md"), "w") as f:
                f.write("x")
            idx = self.b.knowledge_index(hub)
        self.assertIn("_용어집.md", idx)
        self.assertIn("기술/ (1건): 스택 선정", idx)
        self.assertNotIn("빈폴더", idx)


class SubagentHookTest(unittest.TestCase):
    def test_outside_workspace_is_silent(self):
        p = run(SUBAGENT, stdin=json.dumps({"cwd": "/tmp"}))
        self.assertEqual(p.stdout.strip(), "")

    def test_workflow_and_named_agents_are_silent(self):
        """워크플로 워커·일회성 이름 에이전트에는 볼트 안내를 붙이지 않는다."""
        for agent_type in ("workflow-subagent", "mid-arc-b", "opus-verifier"):
            p = run(SUBAGENT, stdin=json.dumps(
                {"cwd": os.path.join(lib.WORKSPACE_ROOT, "projects", "todari"),
                 "agent_type": agent_type}))
            self.assertEqual(p.stdout.strip(), "", agent_type)

    @unittest.skipUnless(HAS_VAULT, "볼트 접근 불가")
    def test_missing_agent_type_still_injects(self):
        """agent_type을 주지 않는 런타임(Codex)은 종류를 모르므로 붙인다."""
        p = run(SUBAGENT, stdin=json.dumps(
            {"cwd": os.path.join(lib.WORKSPACE_ROOT, "projects", "todari")}))
        self.assertIn("vault_search.py", p.stdout)

    @unittest.skipUnless(HAS_VAULT, "볼트 접근 불가")
    def test_injects_readonly_guidance(self):
        p = run(SUBAGENT, stdin=json.dumps(
            {"cwd": os.path.join(lib.WORKSPACE_ROOT, "projects", "todari"),
             "agent_type": "cheap-explorer"}))
        ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("vault_search.py", ctx)
        self.assertIn("쓰지 말 것", ctx)


class OneLinerTest(unittest.TestCase):
    """한 줄 정의는 여러 줄 인용 블록으로 이어질 수 있다."""

    def setUp(self):
        sys.path.insert(0, HERE)
        import obsidian_bridge
        self.b = obsidian_bridge

    def test_multiline_blockquote_is_joined(self):
        body = ("# X\n\n> **한 줄 정의:** KBO 티켓 정가 양도 플랫폼.\n"
                "> 웃돈 거래를 억제한다.\n\n## 다음\n")
        self.assertEqual(self.b.one_liner(body),
                         "KBO 티켓 정가 양도 플랫폼. 웃돈 거래를 억제한다.")

    def test_stops_at_blank_quote_line(self):
        body = "> **한 줄 정의:** 첫 줄\n>\n> 다른 인용\n"
        self.assertEqual(self.b.one_liner(body), "첫 줄")


if __name__ == "__main__":
    unittest.main()
