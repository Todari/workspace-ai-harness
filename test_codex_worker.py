import io
import json
import os
import subprocess
import tempfile
import time
import sys
import unittest

import codex_worker as worker


MANIFEST = {
    "task_class": "medium",
    "objective": "버그를 수정한다",
    "worker_effort": "high",
    "scope": ["src"],
    "acceptance_criteria": ["회귀 테스트가 통과한다"],
    "constraints": ["기존 변경 보존"],
    "verification_commands": ["npm test"],
}


class ModelConfigTest(unittest.TestCase):
    def test_tracked_config_is_valid(self):
        config = worker.load_config()
        self.assertEqual(config["worker"]["model"], "gpt-5.6-sol")
        self.assertEqual(config["planner"]["default_effort"], "medium")
        self.assertEqual(config["routing"]["mode"], "hard_handoff")
        self.assertEqual(worker.model_config_errors(config), [])

    def test_history_must_match_active_route(self):
        config = worker.load_config()
        config["history"][-1]["worker_model"] = "old-model"
        self.assertIn("history 마지막 worker_model가 활성 설정과 다름",
                      worker.model_config_errors(config))

    def test_rejects_invalid_routing_threshold(self):
        config = worker.load_config()
        config["routing"]["max_contract_chars"] = 0
        self.assertIn("routing.max_contract_chars는 1 이상의 정수여야 함",
                      worker.model_config_errors(config))


class ManifestTest(unittest.TestCase):
    def test_normalizes_valid_manifest(self):
        manifest = worker.normalize_manifest(dict(MANIFEST))
        self.assertEqual(manifest["task_mode"], "implement")
        self.assertEqual(manifest["worker_effort"], "high")
        self.assertEqual(manifest["scope"], ["src"])

    def test_optional_constraints_default_to_empty(self):
        data = dict(MANIFEST)
        del data["constraints"]
        self.assertEqual(worker.normalize_manifest(data)["constraints"], [])

    def test_rejects_scope_outside_repo(self):
        data = dict(MANIFEST, scope=["../other"])
        with self.assertRaises(worker.WorkerError):
            worker.normalize_manifest(data)

    def test_loads_manifest_from_stdin(self):
        manifest = worker.load_manifest("-", io.StringIO(json.dumps(MANIFEST)))
        self.assertEqual(manifest["objective"], MANIFEST["objective"])

    def test_inspection_manifest_uses_lower_default_effort(self):
        data = dict(MANIFEST, task_mode="inspect")
        del data["worker_effort"]
        manifest = worker.load_manifest(
            "-", io.StringIO(json.dumps(data)), default_effort="high",
            inspection_effort="medium")
        self.assertEqual(manifest["worker_effort"], "medium")

    def test_contract_size_is_bounded(self):
        data = dict(MANIFEST, objective="x" * 500)
        with self.assertRaises(worker.WorkerError):
            worker.normalize_manifest(data, max_contract_chars=100)


class CommandTest(unittest.TestCase):
    def test_prompt_preserves_repo_rules_and_existing_changes(self):
        prompt = worker.build_prompt(worker.normalize_manifest(MANIFEST))
        self.assertIn("AGENTS.md", prompt)
        self.assertIn("CLAUDE.md", prompt)
        self.assertIn("preserve all\npre-existing user changes", prompt)

    def test_inspection_prompt_forbids_edits(self):
        prompt = worker.build_prompt(worker.normalize_manifest(
            dict(MANIFEST, task_mode="inspect")))
        self.assertIn("read-only investigation worker", prompt)
        self.assertIn("Do not modify any file", prompt)

    def test_new_run_pins_model_effort_and_sandbox(self):
        command = worker.build_command(
            "run", "/repo", "gpt-5.6-sol", "high", "workspace-write", "never",
            "/tmp/result", "prompt")
        self.assertEqual(command[:3], [worker.CODEX_BIN, "exec", "-C"])
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertIn('approval_policy="never"', command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--output-schema", command)
        self.assertEqual(command[-1], "prompt")

    def test_resume_targets_recorded_thread(self):
        command = worker.build_command(
            "resume", "/repo", "gpt-5.6-sol", "xhigh", "workspace-write", "never",
            "/tmp/result", "retry", thread_id="thread-1")
        self.assertEqual(command[:3], [worker.CODEX_BIN, "exec", "resume"])
        self.assertEqual(command[-2:], ["thread-1", "retry"])

    def test_user_config_can_be_enabled_explicitly(self):
        command = worker.build_command(
            "run", "/repo", "gpt-5.6-sol", "high", "workspace-write", "never",
            "/tmp/result", "prompt", ignore_user_config=False)
        self.assertNotIn("--ignore-user-config", command)


class EventParsingTest(unittest.TestCase):
    def test_extracts_thread_and_usage(self):
        stream = "\n".join((
            '{"type":"thread.started","thread_id":"thread-1"}',
            '{"type":"item.completed","item":{"type":"agent_message"}}',
            '{"type":"turn.completed","usage":{"input_tokens":100,'
            '"cached_input_tokens":80,"output_tokens":20,"reasoning_output_tokens":7}}',
        ))
        parsed = worker.parse_jsonl(stream)
        self.assertEqual(parsed["thread_id"], "thread-1")
        self.assertEqual(parsed["usage"]["input_tokens"], 100)
        self.assertEqual(parsed["usage"]["reasoning_output_tokens"], 7)


class ResultValidationTest(unittest.TestCase):
    def test_inspection_result_cannot_claim_changes(self):
        result = {
            "status": "completed", "summary": "done", "changed_files": ["a.py"],
            "tests": [], "risks": [], "next_action": "",
        }
        self.assertIn("inspect 결과의 changed_files는 비어 있어야 함",
                      worker.result_errors(result, "inspect"))


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = worker.RUNS_DIR
        worker.RUNS_DIR = self.tmp.name

    def tearDown(self):
        worker.RUNS_DIR = self.original
        self.tmp.cleanup()

    def test_record_round_trip_and_list(self):
        run_id = "20260903-120000-abcdef"
        record = {
            "run_id": run_id,
            "created_at": "2026-09-03T12:00:00+09:00",
            "mode": "run",
            "parent_run_id": "",
            "repo": "/repo",
            "model": "gpt-5.6-sol",
            "effort": "high",
            "worker_status": "completed",
            "invocation_status": "completed",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        path = worker.write_record(record)
        self.assertEqual(worker.load_record(run_id)["model"], "gpt-5.6-sol")
        self.assertEqual(worker.list_records(1)[0]["run_id"], run_id)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_list_uses_invocation_failure_status(self):
        run_id = "20260903-120001-fedcba"
        worker.write_record({
            "run_id": run_id,
            "created_at": "2026-09-03T12:00:01+09:00",
            "mode": "run",
            "parent_run_id": "",
            "repo": "/repo",
            "model": "gpt-5.6-sol",
            "effort": "high",
            "worker_status": "unknown",
            "invocation_status": "failed",
            "usage": {},
        })
        self.assertEqual(worker.list_records(1)[0]["status"], "failed")


class DryRunTest(unittest.TestCase):
    def test_dry_run_does_not_invoke_codex_or_write_record(self):
        with tempfile.TemporaryDirectory() as workspace:
            repo = os.path.join(workspace, "repo")
            os.mkdir(repo)
            subprocess.run(["git", "init", "-q", repo], check=True)
            old_root = worker.lib.WORKSPACE_ROOT
            old_runs = worker.RUNS_DIR
            worker.lib.WORKSPACE_ROOT = os.path.realpath(workspace)
            worker.RUNS_DIR = os.path.join(workspace, "runs")
            try:
                config = worker.load_config()
                result = worker.execute(
                    "run", worker.resolve_repo(repo), worker.normalize_manifest(MANIFEST),
                    "gpt-5.6-sol", "high", config, 30, dry_run=True)
                self.assertTrue(result["dry_run"])
                self.assertFalse(os.path.exists(worker.RUNS_DIR))
            finally:
                worker.lib.WORKSPACE_ROOT = old_root
                worker.RUNS_DIR = old_runs

    def test_inspection_dry_run_is_read_only(self):
        with tempfile.TemporaryDirectory() as workspace:
            repo = os.path.join(workspace, "repo")
            os.mkdir(repo)
            subprocess.run(["git", "init", "-q", repo], check=True)
            old_root = worker.lib.WORKSPACE_ROOT
            old_runs = worker.RUNS_DIR
            worker.lib.WORKSPACE_ROOT = os.path.realpath(workspace)
            worker.RUNS_DIR = os.path.join(workspace, "runs")
            try:
                config = worker.load_config()
                manifest = worker.normalize_manifest(dict(MANIFEST, task_mode="inspect"))
                result = worker.execute(
                    "run", worker.resolve_repo(repo), manifest, "gpt-5.6-sol", "medium",
                    config, 30, dry_run=True)
                self.assertEqual(result["task_mode"], "inspect")
                self.assertEqual(result["sandbox"], "read-only")
                self.assertIn("read-only", result["command"])
            finally:
                worker.lib.WORKSPACE_ROOT = old_root
                worker.RUNS_DIR = old_runs


class RepoLockTest(unittest.TestCase):
    def test_second_writer_waits_then_times_out(self):
        with tempfile.TemporaryDirectory() as d:
            old_runs = worker.RUNS_DIR
            worker.RUNS_DIR = d
            try:
                with worker.repo_lock("/repo", 0):
                    started = time.monotonic()
                    with self.assertRaises(worker.WorkerError) as ctx:
                        with worker.repo_lock("/repo", 1):
                            pass
                    self.assertGreaterEqual(time.monotonic() - started, 0.9)
                    self.assertIn("대기 초과", str(ctx.exception))
            finally:
                worker.RUNS_DIR = old_runs


class DetachAndWaitTest(unittest.TestCase):
    """가짜 codex 바이너리로 detach → wait 회수 경로를 끝까지 검증한다."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = os.path.realpath(self.temp.name)
        self.repo = os.path.join(self.workspace, "repo")
        os.mkdir(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.fake_codex = os.path.join(self.workspace, "codex")
        with open(self.fake_codex, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n"
                    "sleep 1\n"
                    "out=\"\"\n"
                    "while [ $# -gt 0 ]; do if [ \"$1\" = -o ]; then out=$2; fi; shift; done\n"
                    "printf '%s\\n' '{\"type\":\"thread.started\",\"thread_id\":\"t1\"}'\n"
                    "printf '%s' '{\"status\":\"completed\",\"summary\":\"ok\","
                    "\"changed_files\":[],\"tests\":[],\"risks\":[],\"next_action\":\"\"}' > \"$out\"\n")
        os.chmod(self.fake_codex, 0o755)
        self.env = dict(os.environ,
                        WORKSPACE_HARNESS_ROOT=self.workspace,
                        WORKSPACE_HARNESS_CODEX_RUNS_DIR=os.path.join(self.workspace, "runs"),
                        WORKSPACE_HARNESS_CODEX_BIN=self.fake_codex)
        self.script = os.path.join(os.path.dirname(os.path.abspath(worker.__file__)),
                                   "codex_worker.py")

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, stdin=""):
        proc = subprocess.run([sys.executable, self.script] + list(args), input=stdin,
                              capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_detach_returns_immediately_and_wait_collects_record(self):
        started = time.monotonic()
        launched = self.run_cli("run", "--repo", self.repo, "--manifest", "-", "--detach",
                                "--orchestrator-session", "abc",
                                stdin=json.dumps(MANIFEST))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(launched["status"], "running")
        self.assertIn("wait", launched["wait_command"])
        run_id = launched["run_id"]
        short = self.run_cli("wait", run_id, "--timeout", "0")
        self.assertEqual(short["status"], "running")
        self.assertEqual(short["pending"], [run_id])
        final = self.run_cli("wait", run_id, "--timeout", "30")
        self.assertEqual(final["run_id"], run_id)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["thread_id"], "t1")
        self.assertFalse(os.path.exists(os.path.join(self.workspace, "runs",
                                                     run_id + ".pending.json")))

    def test_wait_rejects_unknown_run(self):
        proc = subprocess.run([sys.executable, self.script, "wait", "20260101-000000-abcdef",
                               "--timeout", "0"], capture_output=True, text=True, env=self.env)
        self.assertEqual(proc.returncode, 2)


class WorkerSlotTest(unittest.TestCase):
    def test_second_worker_times_out_when_only_slot_is_held(self):
        with tempfile.TemporaryDirectory() as d:
            old_runs = worker.RUNS_DIR
            worker.RUNS_DIR = d
            try:
                with worker.worker_slot(1, 1):
                    with self.assertRaises(worker.WorkerError):
                        with worker.worker_slot(1, 0):
                            pass
            finally:
                worker.RUNS_DIR = old_runs


if __name__ == "__main__":
    unittest.main()
