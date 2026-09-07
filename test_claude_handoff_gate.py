import json
import os
import tempfile
import unittest

import claude_handoff_gate as gate
import codex_worker


class ClassifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = codex_worker.load_config()

    def route(self, prompt):
        return gate.classify_prompt(prompt, self.config)[0]

    def test_routes_code_changes_to_implementation(self):
        self.assertEqual(self.route("로그인 버그를 수정하고 테스트 추가해줘"), "implement")

    def test_routes_repo_diagnosis_to_read_only_worker(self):
        self.assertEqual(self.route("이 코드에서 빌드 실패 원인을 분석해줘"), "inspect")

    def test_routes_architecture_decision_to_fable_architect(self):
        self.assertEqual(self.route("인증 구조를 어떻게 설계할지 정해줘"), "architect")

    def test_routes_short_followup_to_implementation(self):
        self.assertEqual(self.route("응, 그렇게 해줘"), "implement")

    def test_routes_multi_repo_change_to_batch(self):
        self.assertEqual(self.route("모든 레포의 설정을 업데이트해줘"), "batch")

    def test_routes_multi_repo_inspection_to_batch(self):
        self.assertEqual(self.route("여러 레포의 테스트 실패 원인을 조사해줘"), "batch")

    def test_explicit_opt_out_wins(self):
        self.assertEqual(self.route("코드를 수정하되 Codex 쓰지 마"), "direct")

    def test_conversation_stays_direct(self):
        self.assertEqual(self.route("이 의사결정의 장단점을 설명해줘"), "direct")

    def test_question_about_improvement_does_not_trigger_implementation(self):
        self.assertEqual(self.route("지금 설계와 개선 방향이 맞아?"), "direct")

    def test_followup_inherits_explicit_opt_out(self):
        previous = {"route": "direct", "reason": "explicit-opt-out"}
        self.assertEqual(gate.classify_prompt("응 그렇게 진행해줘", self.config, previous),
                         ("direct", "follow-up-inherits-opt-out"))
        chained = {"route": "direct", "reason": "follow-up-inherits-opt-out"}
        self.assertEqual(gate.classify_prompt("해줘", self.config, chained)[0], "direct")
        self.assertEqual(gate.classify_prompt(
            "응 그렇게 진행해줘", self.config, {"route": "direct", "reason": "short-question"})[0],
            "implement")

    def test_question_mark_beats_action_verb(self):
        self.assertEqual(self.route("이제 하네스가 효율적인 작업을 토큰을 아끼면서 진행해?"), "direct")
        self.assertEqual(self.route("이 구조로 리팩터링하면 성능이 좋아져?"), "direct")

    def test_question_mark_request_still_delegates(self):
        self.assertEqual(self.route("로그인 버그 수정해줄래?"), "implement")

    def test_bypass_phrase_without_space(self):
        self.assertEqual(self.route("직접해줘. 커밋해줘."), "direct")

    def test_short_code_question_stays_direct(self):
        self.assertEqual(self.route("이 함수 왜 이렇게 짰어?"), "direct")
        self.assertEqual(self.route("이 코드 왜 테스트가 실패하는지 알려줘"), "direct")

    def test_single_file_reference_stays_direct(self):
        self.assertEqual(self.route("src/auth/login.ts 흐름 리뷰해줘"), "direct")

    def test_single_file_change_request_still_delegates(self):
        self.assertEqual(self.route("src/auth/login.ts의 만료 처리를 수정해줘"), "implement")

    def test_long_investigation_still_delegates(self):
        self.assertEqual(self.route(
            "결제 모듈과 주문 모듈 사이에서 웹훅이 두 번 처리되는 원인을 레포 전체 흐름을 따라가며 "
            "분석하고 어디서 중복이 생기는지 근거와 함께 정리해줘"), "inspect")


class GateDecisionTest(unittest.TestCase):
    def setUp(self):
        self.config = codex_worker.load_config()
        self.state = {
            "session": "abc",
            "route": "implement",
            "phase": "planning",
            "planner_tool_calls": 0,
            "worker_calls": 0,
            "violations": 0,
            "stop_blocks": 0,
        }

    def test_allows_only_bounded_git_prep(self):
        for _ in range(self.config["routing"]["max_planner_tool_calls"]):
            self.assertIsNone(gate.pre_tool(
                self.state, "Bash", {"command": "git status --short --branch"}, self.config))
        denied = gate.pre_tool(
            self.state, "Bash", {"command": "git status --short --branch"}, self.config)
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

    RUN = ("python3 ~/.claude/hooks/workspace-harness/codex_worker.py run "
           "--repo /repo --orchestrator-session abc --manifest - --detach "
           "<<'JSON'\n{}\nJSON")
    WAIT = ("python3 ~/.claude/hooks/workspace-harness/codex_worker.py wait "
            "20260907-120000-abcdef --timeout 540")

    def test_worker_run_transitions_to_delegated(self):
        self.assertIsNone(gate.pre_tool(self.state, "Bash", {"command": self.RUN}, self.config))
        self.assertEqual(self.state["phase"], "delegated")
        self.assertEqual(self.state["worker_calls"], 1)

    def test_run_without_detach_is_denied(self):
        command = self.RUN.replace(" --detach", "")
        decision = gate.pre_tool(self.state, "Bash", {"command": command}, self.config)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("--detach", decision["hookSpecificOutput"]["permissionDecisionReason"])

    def test_background_parameter_is_denied_even_for_valid_run(self):
        decision = gate.pre_tool(
            self.state, "Bash", {"command": self.RUN, "run_in_background": True}, self.config)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.state["background_attempts"], 1)
        self.assertEqual(self.state["phase"], "planning")

    def test_detached_run_then_wait_until_result(self):
        gate.pre_tool(self.state, "Bash", {"command": self.RUN}, self.config)
        gate.post_tool(self.state, "Bash", {"command": self.RUN},
                       '{"run_id":"20260907-120000-abcdef","status":"running"}')
        self.assertEqual(self.state["phase"], "delegated")
        self.assertEqual(self.state["run_ids"], ["20260907-120000-abcdef"])
        self.assertIsNone(gate.pre_tool(self.state, "Bash", {"command": self.WAIT}, self.config))
        gate.post_tool(self.state, "Bash", {"command": self.WAIT},
                       '{"status":"running","pending":["20260907-120000-abcdef"]}')
        self.assertEqual(self.state["phase"], "delegated")
        self.assertIsNone(gate.pre_tool(self.state, "Bash", {"command": self.WAIT}, self.config))
        gate.post_tool(self.state, "Bash", {"command": self.WAIT},
                       '{"run_id":"20260907-120000-abcdef","status":"completed"}')
        self.assertEqual(self.state["phase"], "result_ready")
        self.assertEqual(self.state["wait_calls"], 2)
        self.assertFalse(gate.pre_tool(self.state, "Bash", {"command": self.WAIT},
                                       self.config)["continue"])

    def test_advisory_enforcement_never_denies(self):
        config = json.loads(json.dumps(self.config))
        config["routing"]["enforcement"] = "advisory"
        self.assertIsNone(gate.pre_tool(self.state, "Edit", {"file_path": "a.py"}, config))
        self.assertIsNone(gate.stop_decision(self.state, config))
        self.assertIn("CODEX_IMPLEMENT", gate.route_context("implement", self.state, config))

    def test_wait_before_run_is_denied(self):
        decision = gate.pre_tool(self.state, "Bash", {"command": self.WAIT}, self.config)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_wait_call_limit_hard_stops(self):
        self.state.update({"phase": "delegated", "worker_calls": 1,
                           "wait_calls": self.config["routing"]["max_wait_calls"]})
        decision = gate.pre_tool(self.state, "Bash", {"command": self.WAIT}, self.config)
        self.assertFalse(decision["continue"])

    def test_wait_with_chained_command_is_denied(self):
        self.state.update({"phase": "delegated", "worker_calls": 1})
        decision = gate.pre_tool(
            self.state, "Bash", {"command": self.WAIT + " && cat run.log"}, self.config)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_violations_soft_deny_until_threshold(self):
        limit = self.config["routing"]["max_route_violations"]
        for _ in range(limit):
            decision = gate.pre_tool(self.state, "Read", {"file_path": "a.py"}, self.config)
            self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("--detach", decision["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertFalse(gate.pre_tool(self.state, "Read", {"file_path": "a.py"},
                                       self.config)["continue"])

    def test_background_or_redirected_worker_is_denied(self):
        command = ("python3 codex_worker.py run --repo /repo --orchestrator-session abc "
                   "--manifest - > run.log")
        decision = gate.pre_tool(self.state, "Bash", {"command": command}, self.config)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_missing_or_wrong_orchestrator_session_is_denied(self):
        for session in ("", "wrong"):
            flag = " --orchestrator-session %s" % session if session else ""
            command = ("python3 codex_worker.py run --repo /repo%s --manifest - "
                       "<<'JSON'\n{}\nJSON") % flag
            fresh = dict(self.state)
            decision = gate.pre_tool(fresh, "Bash", {"command": command}, self.config)
            self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_chained_command_is_denied(self):
        command = ("python3 codex_worker.py run --repo /repo --orchestrator-session abc "
                   "--manifest -; rg TODO <<'JSON'\n{}\nJSON")
        decision = gate.pre_tool(self.state, "Bash", {"command": command}, self.config)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_same_turn_resume_is_denied(self):
        self.state.update({"phase": "result_ready", "worker_calls": 1})
        command = "python3 codex_worker.py resume 20260907-120000-abcdef --instruction retry"
        decision = gate.pre_tool(self.state, "Bash", {"command": command}, self.config)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_polling_while_worker_runs_is_denied_with_wait_hint(self):
        self.state.update({"phase": "delegated", "worker_calls": 1})
        decision = gate.pre_tool(self.state, "Bash", {"command": "ps aux"}, self.config)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("wait", decision["hookSpecificOutput"]["permissionDecisionReason"])

    def test_post_worker_result_blocks_further_tools(self):
        self.state.update({"phase": "delegated", "worker_calls": 1})
        gate.post_tool(
            self.state, "Bash", {"command": "python3 codex_worker.py run --manifest -"},
            '{"run_id":"20260907-120000-abcdef","status":"completed"}')
        self.assertEqual(self.state["phase"], "result_ready")
        self.assertEqual(self.state["run_ids"], ["20260907-120000-abcdef"])
        decision = gate.pre_tool(self.state, "Read", {"file_path": "src/a.py"}, self.config)
        self.assertFalse(decision["continue"])

    def test_failed_start_returns_slot_for_retry(self):
        # 매니페스트 검증 실패: run_id 없이 {"status":"error"}만 돌아온다.
        self.assertIsNone(gate.pre_tool(self.state, "Bash", {"command": self.RUN}, self.config))
        gate.post_tool(self.state, "Bash", {"command": self.RUN},
                       '{"status": "error", "error": "유효하지 않은 task_class: bugfix"}')
        self.assertEqual(self.state["phase"], "planning")
        self.assertEqual(self.state["worker_calls"], 0)
        self.assertEqual(self.state["failed_starts"], 1)
        self.assertIsNone(gate.pre_tool(self.state, "Bash", {"command": self.RUN}, self.config))
        self.assertEqual(self.state["phase"], "delegated")
        self.assertEqual(self.state["worker_calls"], 1)

    def test_denied_start_without_post_event_returns_slot(self):
        # 자동 모드 분류기가 거부하면 PostToolUse가 오지 않을 수 있다. 회수할 run_id가 없으면 재시도 허용.
        self.assertIsNone(gate.pre_tool(self.state, "Bash", {"command": self.RUN}, self.config))
        self.assertIsNone(gate.pre_tool(self.state, "Bash", {"command": self.RUN}, self.config))
        self.assertEqual(self.state["worker_calls"], 1)
        self.assertEqual(self.state["failed_starts"], 1)

    def test_failed_start_retry_is_capped(self):
        for _ in range(self.config["routing"]["max_launch_failures"] + 1):
            self.assertIsNone(gate.pre_tool(self.state, "Bash", {"command": self.RUN}, self.config))
        denied = gate.pre_tool(self.state, "Bash", {"command": self.RUN}, self.config)
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_detached_run_keeps_slot(self):
        self.assertIsNone(gate.pre_tool(self.state, "Bash", {"command": self.RUN}, self.config))
        gate.post_tool(self.state, "Bash", {"command": self.RUN},
                       '{"run_id":"20260907-120000-abcdef","status":"running"}')
        denied = gate.pre_tool(self.state, "Bash", {"command": self.RUN}, self.config)
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.state["worker_calls"], 1)

    def test_stop_requires_one_handoff_attempt(self):
        first = gate.stop_decision(self.state)
        second = gate.stop_decision(self.state)
        self.assertEqual(first["decision"], "block")
        self.assertIsNone(second)


class StatePersistenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = gate.STATE_DIR
        gate.STATE_DIR = self.temp.name
        self.config = codex_worker.load_config()

    def tearDown(self):
        gate.STATE_DIR = self.original
        self.temp.cleanup()

    def test_user_prompt_stores_classification_without_prompt(self):
        workspace = os.path.realpath(os.path.join(self.temp.name, "workspace"))
        data = {
            "hook_event_name": "UserPromptSubmit",
            "cwd": os.path.join(workspace, "projects", "example"),
            "session_id": "session-secret",
            "prompt": "민감한 상세 코드 요청을 구현해줘",
        }
        old_root = gate.lib.WORKSPACE_ROOT
        gate.lib.WORKSPACE_ROOT = workspace
        try:
            output = gate.handle(data, self.config)
        finally:
            gate.lib.WORKSPACE_ROOT = old_root
        self.assertIn("CODEX_IMPLEMENT", output["hookSpecificOutput"]["additionalContext"])
        files = [name for name in os.listdir(self.temp.name) if name.endswith(".json")]
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.temp.name, files[0]), encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("민감한", content)
        self.assertEqual(json.loads(content)["route"], "implement")


if __name__ == "__main__":
    unittest.main()
