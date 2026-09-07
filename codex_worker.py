#!/usr/bin/env python3
"""Fable가 만든 작업 계약을 전용 Codex 세션에서 실행하고 재현 가능한 이력을 남긴다.

사용:
  python3 codex_worker.py run --repo /path/to/repo --manifest task.json
  python3 codex_worker.py run --repo /path/to/repo --manifest - --detach   # 즉시 run_id 반환
  python3 codex_worker.py wait RUN_ID [RUN_ID ...] --timeout 540        # 기록 완료까지 blocking 대기
  python3 codex_worker.py resume RUN_ID --instruction "실패 원인을 고치고 다시 검증"
  python3 codex_worker.py rerun RUN_ID --model gpt-5.6-sol --effort xhigh
  python3 codex_worker.py list --limit 10
  python3 codex_worker.py show RUN_ID
"""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time

import harness_lib as lib

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "orchestration", "models.json")
RESULT_SCHEMA_PATH = os.path.join(HERE, "orchestration", "codex-result.schema.json")
RUNS_DIR = lib.CODEX_RUNS_DIR
CODEX_BIN = os.environ.get("WORKSPACE_HARNESS_CODEX_BIN", "codex")
RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
TASK_CLASSES = frozenset(("simple", "medium", "complex", "high_risk", "batch_or_repo_wide"))
TASK_MODES = frozenset(("implement", "inspect"))
EFFORTS = frozenset(("none", "low", "medium", "high", "xhigh", "max"))
MANIFEST_KEYS = frozenset((
    "task_mode", "task_class", "objective", "worker_effort", "scope",
    "acceptance_criteria", "constraints", "verification_commands",
))
MAX_STATUS_LINES = 200
MAX_RUN_RECORDS = 200


class WorkerError(Exception):
    pass


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path, label):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise WorkerError("%s 읽기 실패: %s" % (label, exc))
    if not isinstance(data, dict):
        raise WorkerError("%s는 JSON object여야 함" % label)
    return data


def model_config_errors(data):
    errors = []
    if data.get("schema_version") != 2:
        errors.append("schema_version은 2여야 함")
    if not isinstance(data.get("last_reviewed"), str):
        errors.append("last_reviewed 날짜 누락")
    if not isinstance(data.get("review_interval_days"), int):
        errors.append("review_interval_days 정수 누락")
    planner = data.get("planner")
    worker = data.get("worker")
    routing = data.get("routing")
    history = data.get("history")
    if not isinstance(planner, dict):
        errors.append("planner object 누락")
    else:
        for key in ("model", "runtime_model", "default_effort", "escalation_effort",
                    "critical_effort", "tested_cli_version"):
            if not isinstance(planner.get(key), str) or not planner[key]:
                errors.append("planner.%s 누락" % key)
        for key in ("default_effort", "escalation_effort", "critical_effort"):
            if planner.get(key) not in EFFORTS:
                errors.append("planner.%s 값이 유효하지 않음" % key)
        turns = planner.get("max_turns_before_handoff")
        if not isinstance(turns, int) or isinstance(turns, bool) or turns < 1:
            errors.append("planner.max_turns_before_handoff는 1 이상의 정수여야 함")
    if not isinstance(worker, dict):
        errors.append("worker object 누락")
    else:
        for key in ("model", "default_effort", "inspection_effort",
                    "escalation_effort", "sandbox", "inspection_sandbox",
                    "approval_policy", "tested_cli_version"):
            if not isinstance(worker.get(key), str) or not worker[key]:
                errors.append("worker.%s 누락" % key)
        for key in ("default_effort", "inspection_effort", "escalation_effort"):
            if worker.get(key) not in EFFORTS:
                errors.append("worker.%s 값이 유효하지 않음" % key)
        if not isinstance(worker.get("ignore_user_config"), bool):
            errors.append("worker.ignore_user_config boolean 누락")
    if not isinstance(routing, dict):
        errors.append("routing object 누락")
    else:
        if routing.get("mode") != "hard_handoff":
            errors.append("routing.mode는 hard_handoff여야 함")
        if routing.get("enforcement", "hard") not in ("hard", "advisory"):
            errors.append("routing.enforcement는 hard 또는 advisory여야 함")
        for key in ("classifier_version", "max_contract_chars",
                    "max_planner_tool_calls", "max_route_violations",
                    "max_batch_worker_calls", "max_concurrent_workers",
                    "worker_queue_timeout_seconds", "worker_timeout_seconds",
                    "wait_timeout_seconds", "max_wait_calls", "max_launch_failures"):
            value = routing.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                errors.append("routing.%s는 1 이상의 정수여야 함" % key)
        classes = routing.get("delegate_task_classes")
        if (not isinstance(classes, list) or not classes
                or any(item not in TASK_CLASSES for item in classes)):
            errors.append("routing.delegate_task_classes가 유효하지 않음")
        phrases = routing.get("bypass_phrases")
        if (not isinstance(phrases, list) or not phrases
                or any(not isinstance(item, str) or not item.strip() for item in phrases)):
            errors.append("routing.bypass_phrases가 유효하지 않음")
        if not isinstance(routing.get("forced_command"), str) or not routing["forced_command"]:
            errors.append("routing.forced_command 누락")
    if not isinstance(history, list) or not history:
        errors.append("history는 한 개 이상의 항목이 필요함")
    elif isinstance(planner, dict) and isinstance(worker, dict):
        latest = history[-1]
        if not isinstance(latest, dict):
            errors.append("마지막 history 항목은 object여야 함")
        else:
            expected = {
                "planner_model": planner.get("model"),
                "planner_effort": planner.get("default_effort"),
                "worker_model": worker.get("model"),
                "worker_effort": worker.get("default_effort"),
                "routing_mode": routing.get("mode") if isinstance(routing, dict) else None,
            }
            for key, value in expected.items():
                if latest.get(key) != value:
                    errors.append("history 마지막 %s가 활성 설정과 다름" % key)
    return errors


def load_config(path=CONFIG_PATH):
    data = read_json(path, "모델 설정")
    errors = model_config_errors(data)
    if errors:
        raise WorkerError("모델 설정 오류: " + "; ".join(errors))
    return data


def load_manifest(path, stdin=None, default_effort="high", inspection_effort="medium",
                  max_contract_chars=0):
    try:
        if path == "-":
            data = json.load(stdin or sys.stdin)
        else:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
    except (OSError, ValueError) as exc:
        raise WorkerError("작업 계약 읽기 실패: %s" % exc)
    effort = inspection_effort if data.get("task_mode") == "inspect" else default_effort
    return normalize_manifest(data, effort, max_contract_chars)


def _string_list(data, key, required=True):
    value = data.get(key)
    if value is None and not required:
        return []
    if not isinstance(value, list) or (required and not value):
        raise WorkerError("%s는%s 문자열 배열이어야 함" % (
            key, " 비어 있지 않은" if required else ""))
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise WorkerError("%s의 모든 항목은 비어 있지 않은 문자열이어야 함" % key)
    return [item.strip() for item in value]


def normalize_manifest(data, default_effort="high", max_contract_chars=0):
    if not isinstance(data, dict):
        raise WorkerError("작업 계약은 JSON object여야 함")
    unknown = sorted(set(data) - MANIFEST_KEYS)
    if unknown:
        raise WorkerError("알 수 없는 작업 계약 필드: %s" % ", ".join(unknown))
    objective = data.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        raise WorkerError("objective는 비어 있지 않은 문자열이어야 함")
    task_class = data.get("task_class", "medium")
    if task_class not in TASK_CLASSES:
        raise WorkerError("유효하지 않은 task_class: %s" % task_class)
    effort = data.get("worker_effort", default_effort)
    if effort not in EFFORTS:
        raise WorkerError("유효하지 않은 worker_effort: %s" % effort)
    scope = _string_list(data, "scope")
    for item in scope:
        normalized = os.path.normpath(item)
        if os.path.isabs(item) or normalized == ".." or normalized.startswith(".." + os.sep):
            raise WorkerError("scope는 repo 상대 경로여야 함: %s" % item)
    task_mode = data.get("task_mode", "implement")
    if task_mode not in TASK_MODES:
        raise WorkerError("유효하지 않은 task_mode: %s" % task_mode)
    normalized = {
        "task_mode": task_mode,
        "task_class": task_class,
        "objective": objective.strip(),
        "worker_effort": effort,
        "scope": scope,
        "acceptance_criteria": _string_list(data, "acceptance_criteria"),
        "constraints": _string_list(data, "constraints", required=False),
        "verification_commands": _string_list(data, "verification_commands"),
    }
    contract_chars = len(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")))
    if max_contract_chars and contract_chars > max_contract_chars:
        raise WorkerError("작업 계약이 %d자 상한을 초과함: %d자" % (
            max_contract_chars, contract_chars))
    return normalized


def _git(repo, *args):
    try:
        proc = subprocess.run(["git", "-C", repo] + list(args), capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkerError("git 실행 실패: %s" % exc)
    if proc.returncode != 0:
        raise WorkerError("git %s 실패" % " ".join(args))
    return proc.stdout.strip()


def resolve_repo(path):
    candidate = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(candidate):
        raise WorkerError("repo 경로 없음: %s" % candidate)
    if not lib.in_workspace(candidate):
        raise WorkerError("workspace 밖 repo는 실행할 수 없음: %s" % candidate)
    root = os.path.realpath(_git(candidate, "rev-parse", "--show-toplevel"))
    if not lib.in_workspace(root):
        raise WorkerError("git 루트가 workspace 밖임: %s" % root)
    return root


def _git_optional(repo, *args):
    """커밋이 없는 새 레포처럼 실패가 정상인 조회는 빈 문자열로 처리한다."""
    try:
        return _git(repo, *args)
    except WorkerError:
        return ""


def git_snapshot(repo):
    status = _git(repo, "status", "--short").splitlines()
    return {
        "branch": _git(repo, "branch", "--show-current") or "(detached)",
        "head": _git_optional(repo, "rev-parse", "--verify", "--quiet", "HEAD"),
        "status": status[:MAX_STATUS_LINES],
        "status_truncated": len(status) > MAX_STATUS_LINES,
    }


def manifest_hash(manifest):
    raw = json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_prompt(manifest):
    if manifest["task_mode"] == "inspect":
        role = "You are the sole read-only investigation worker for this repository."
        action = ("Investigate the task contract and report evidence. Do not modify any "
                  "file. In the final JSON, changed_files MUST be an empty array; list the "
                  "files you examined inside summary instead.")
    else:
        role = "You are the sole implementation worker for this repository."
        action = "Implement the task contract below."
    return """%s

%s Inspect only what is needed, preserve all
pre-existing user changes, and stay inside the declared scope. Do not create
subagents. Do not commit, push, open a PR, deploy, or perform unrelated refactors.
Read the applicable repository AGENTS.md first; if none exists, read CLAUDE.md.
Run the declared verification commands when safe and relevant. If a constraint
or missing decision prevents safe completion, return blocked instead of guessing.

Task contract:
%s

Return only the JSON object required by the supplied output schema. Report every
changed file and every verification command, including commands not run and why.
""" % (role, action, json.dumps(manifest, ensure_ascii=False, indent=2))


def build_command(mode, repo, model, effort, sandbox, approval_policy, output_path,
                  prompt, thread_id="", ignore_user_config=True):
    common = [
        "--json", "-m", model,
        "-c", 'model_reasoning_effort="%s"' % effort,
        "-c", 'approval_policy="%s"' % approval_policy,
        "-c", 'sandbox_mode="%s"' % sandbox,
        "--output-schema", RESULT_SCHEMA_PATH,
        "-o", output_path,
    ]
    if ignore_user_config:
        common.insert(0, "--ignore-user-config")
    if mode == "resume":
        if not thread_id:
            raise WorkerError("resume에는 thread_id가 필요함")
        return [CODEX_BIN, "exec", "resume"] + common + [thread_id, prompt]
    return ([CODEX_BIN, "exec", "-C", repo, "--sandbox", sandbox] + common
            + [prompt])


def parse_jsonl(text):
    thread_id = ""
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    event_counts = {}
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type", "unknown")
        event_counts[kind] = event_counts.get(kind, 0) + 1
        if kind == "thread.started" and event.get("thread_id"):
            thread_id = event["thread_id"]
        if kind == "turn.completed" and isinstance(event.get("usage"), dict):
            for key in usage:
                value = event["usage"].get(key, 0)
                if isinstance(value, int):
                    usage[key] += value
    return {"thread_id": thread_id, "usage": usage, "event_counts": event_counts}


def result_errors(data, task_mode="implement"):
    if not isinstance(data, dict):
        return ["최종 결과가 JSON object가 아님"]
    errors = []
    if data.get("status") not in ("completed", "blocked", "needs_review"):
        errors.append("유효하지 않은 status")
    for key in ("summary", "next_action"):
        if not isinstance(data.get(key), str):
            errors.append("%s 문자열 누락" % key)
    for key in ("changed_files", "tests", "risks"):
        if not isinstance(data.get(key), list):
            errors.append("%s 배열 누락" % key)
    if task_mode == "inspect" and data.get("changed_files"):
        errors.append("inspect 결과의 changed_files는 비어 있어야 함")
    return errors


def codex_version():
    try:
        proc = subprocess.run([CODEX_BIN, "--version"], capture_output=True, text=True,
                              timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    return proc.stdout.strip() if proc.returncode == 0 else "unavailable"


def new_run_id():
    return "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(3))


def ensure_runs_dir():
    os.makedirs(RUNS_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(RUNS_DIR, 0o700)
    except OSError:
        pass


def record_path(run_id):
    if not RUN_ID_RE.match(run_id):
        raise WorkerError("유효하지 않은 run ID: %s" % run_id)
    return os.path.join(RUNS_DIR, run_id + ".json")


def write_record(record):
    ensure_runs_dir()
    path = record_path(record["run_id"])
    temp_path = path + ".tmp-" + secrets.token_hex(3)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        with os.fdopen(os.open(temp_path, flags, 0o600), "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp_path, path)
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
    prune_records()
    return path


def prune_records(limit=MAX_RUN_RECORDS):
    try:
        paths = sorted(
            (os.path.join(RUNS_DIR, name) for name in os.listdir(RUNS_DIR)
             if RUN_ID_RE.match(name[:-5]) and name.endswith(".json")),
            key=os.path.getmtime,
            reverse=True,
        )
    except OSError:
        return
    for path in paths[limit:]:
        try:
            os.remove(path)
        except OSError:
            pass


def load_record(run_id):
    return read_json(record_path(run_id), "실행 이력")


@contextlib.contextmanager
def repo_lock(repo, wait_seconds=0):
    """같은 repo에는 writer 하나만 둔다. 다른 writer가 있으면 즉시 실패하지 않고 상한까지 기다린다."""
    ensure_runs_dir()
    lock_name = hashlib.sha256(repo.encode("utf-8")).hexdigest()[:16] + ".lock"
    lock_path = os.path.join(RUNS_DIR, lock_name)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + wait_seconds
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WorkerError("같은 repo에서 다른 Codex writer가 실행 중임 (%d초 대기 초과)"
                                      % wait_seconds)
                time.sleep(1)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def worker_slot(limit, wait_seconds):
    """전역 worker 수를 제한하되 대기는 이 프로세스가 담당해 Claude 폴링을 없앤다."""
    ensure_runs_dir()
    deadline = time.monotonic() + wait_seconds
    while True:
        for index in range(limit):
            path = os.path.join(RUNS_DIR, ".worker-slot-%d.lock" % index)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            try:
                yield index
            finally:
                os.close(fd)
            return
        if time.monotonic() >= deadline:
            raise WorkerError("Codex worker 대기열 제한 시간 초과")
        time.sleep(1)


def _failure_summary(stderr):
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1][:500] if lines else "Codex 실행 실패"


def execute(mode, repo, manifest, model, effort, config, timeout, parent_run_id="",
            thread_id="", instruction="", dry_run=False, parent_record=None,
            ignore_user_config=None, orchestrator_session="", run_id=""):
    run_id = run_id or new_run_id()
    prompt = instruction if mode == "resume" else build_prompt(manifest)
    worker = config["worker"]
    if ignore_user_config is None:
        ignore_user_config = worker["ignore_user_config"]
    placeholder = os.path.join(RUNS_DIR, run_id + ".result.json")
    sandbox = (worker["inspection_sandbox"] if manifest["task_mode"] == "inspect"
               else worker["sandbox"])
    command = build_command(mode, repo, model, effort, sandbox,
                            worker["approval_policy"], placeholder, prompt, thread_id,
                            ignore_user_config)
    if dry_run:
        return {
            "dry_run": True,
            "run_id": run_id,
            "mode": mode,
            "repo": repo,
            "model": model,
            "effort": effort,
            "task_mode": manifest["task_mode"],
            "sandbox": sandbox,
            "manifest_sha256": manifest_hash(manifest),
            "ignore_user_config": ignore_user_config,
            "command": command[:-1] + ["<prompt>"],
        }

    before = git_snapshot(repo)
    parent_context = {}
    if parent_record:
        parent_head = parent_record.get("git_before", {}).get("head", "")
        parent_context = {
            "original_head": parent_head,
            "current_head": before["head"],
            "head_matches_original": bool(parent_head and parent_head == before["head"]),
            "manifest_matches_original": (
                parent_record.get("manifest_sha256") == manifest_hash(manifest)),
        }
    started_at = now_iso()
    started = time.monotonic()
    queue_seconds = 0.0
    invocation_status = "failed"
    exit_code = None
    error = ""
    parsed = {"thread_id": thread_id, "usage": {}, "event_counts": {}}
    result = None
    ensure_runs_dir()
    with tempfile.TemporaryDirectory(prefix=".%s-" % run_id, dir=RUNS_DIR) as temp_dir:
        result_path = os.path.join(temp_dir, "result.json")
        command = build_command(mode, repo, model, effort, sandbox,
                                worker["approval_policy"], result_path, prompt, thread_id,
                                ignore_user_config)
        try:
            queue_started = time.monotonic()
            with worker_slot(config["routing"]["max_concurrent_workers"],
                             config["routing"]["worker_queue_timeout_seconds"]):
                queue_seconds = time.monotonic() - queue_started
                with repo_lock(repo, config["routing"]["worker_queue_timeout_seconds"]):
                    proc = subprocess.run(command, cwd=repo, capture_output=True, text=True,
                                          timeout=timeout)
            exit_code = proc.returncode
            parsed = parse_jsonl(proc.stdout)
            if not parsed["thread_id"]:
                parsed["thread_id"] = thread_id
            if proc.returncode == 0:
                try:
                    result = read_json(result_path, "Codex 최종 결과")
                    errors = result_errors(result, manifest["task_mode"])
                    if errors:
                        error = "; ".join(errors)
                    else:
                        invocation_status = "completed"
                except WorkerError as exc:
                    error = str(exc)
            else:
                error = _failure_summary(proc.stderr)
        except subprocess.TimeoutExpired:
            invocation_status = "timeout"
            error = "%d초 제한 시간 초과" % timeout
        except OSError as exc:
            error = "Codex 실행 실패: %s" % exc
        except WorkerError as exc:
            error = str(exc)
    finished_at = now_iso()
    after = git_snapshot(repo)
    record = {
        "schema_version": 1,
        "run_id": run_id,
        "mode": mode,
        "parent_run_id": parent_run_id,
        "parent_context": parent_context,
        "thread_id": parsed["thread_id"],
        "created_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(time.monotonic() - started, 3),
        "queue_seconds": round(queue_seconds, 3),
        "repo": repo,
        "task_mode": manifest["task_mode"],
        "orchestrator_session": orchestrator_session[:64],
        "model": model,
        "effort": effort,
        "sandbox": sandbox,
        "approval_policy": worker["approval_policy"],
        "ignore_user_config": ignore_user_config,
        "codex_cli_version": codex_version(),
        "prompt_version": 2,
        "manifest_sha256": manifest_hash(manifest),
        "manifest": manifest,
        "resume_instruction": instruction,
        "git_before": before,
        "git_after": after,
        "invocation_status": invocation_status,
        "worker_status": result.get("status") if result else "unknown",
        "exit_code": exit_code,
        "usage": parsed["usage"],
        "event_counts": parsed["event_counts"],
        "result": result,
        "error": error,
    }
    path = write_record(record)
    total = (parsed["usage"].get("input_tokens", 0)
             + parsed["usage"].get("output_tokens", 0))
    lib.event("codex-worker", parsed["thread_id"], "%s/%s:%s:%d" % (
        model, effort, record["worker_status"], total))
    return public_result(record, path)


def public_result(record, path):
    status = record["worker_status"] if record["invocation_status"] == "completed" else record["invocation_status"]
    return {
        "run_id": record["run_id"],
        "status": status,
        "thread_id": record["thread_id"],
        "model": record["model"],
        "effort": record["effort"],
        "duration_seconds": record["duration_seconds"],
        "queue_seconds": record.get("queue_seconds", 0),
        "task_mode": record.get("task_mode", "implement"),
        "usage": record["usage"],
        "result": record["result"],
        "error": record["error"],
        "record_path": path,
        "parent_context": record.get("parent_context", {}),
    }


def pending_path(run_id):
    return record_path(run_id)[:-5] + ".pending.json"


def wait_command(run_ids, timeout):
    return "python3 %s wait %s --timeout %d" % (
        os.path.abspath(__file__), " ".join(run_ids), timeout)


def start_detached(repo, manifest, model, effort, config, timeout, ignore_user_config,
                   orchestrator_session):
    """worker를 세션과 분리된 프로세스로 띄우고 run_id만 즉시 돌려준다.

    Claude Bash 도구의 실행 상한(기본 600초)보다 Codex run이 길어도 결과 기록이 유실되지 않고,
    Claude는 `wait`로 한 번씩 blocking 대기만 하면 된다.
    """
    ensure_runs_dir()
    run_id = new_run_id()
    pending = {
        "run_id": run_id,
        "created_at": now_iso(),
        "repo": repo,
        "manifest": manifest,
        "model": model,
        "effort": effort,
        "timeout": timeout,
        "ignore_user_config": ignore_user_config,
        "orchestrator_session": orchestrator_session[:64],
    }
    path = pending_path(run_id)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w",
                   encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False)
    devnull = open(os.devnull, "r+b")
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "_exec-pending", run_id],
            stdin=devnull, stdout=devnull, stderr=devnull, cwd=repo,
            start_new_session=True, close_fds=True)
    finally:
        devnull.close()
    wait_timeout = config["routing"]["wait_timeout_seconds"]
    return {
        "run_id": run_id,
        "status": "running",
        "model": model,
        "effort": effort,
        "task_mode": manifest["task_mode"],
        "wait_command": wait_command([run_id], wait_timeout),
    }


def exec_pending(run_id):
    """detach된 자식 프로세스 본체. 완료 기록을 남기고 pending 파일을 지운다."""
    path = pending_path(run_id)
    pending = read_json(path, "대기 중 실행")
    config = load_config()
    try:
        execute("run", pending["repo"], pending["manifest"], pending["model"],
                pending["effort"], config, pending["timeout"],
                ignore_user_config=pending.get("ignore_user_config"),
                orchestrator_session=pending.get("orchestrator_session", ""),
                run_id=run_id)
    except WorkerError as exc:
        # 실행 전 단계(잠금 대기 초과 등)에서 실패해도 wait가 결과를 받을 수 있게 기록을 남긴다.
        write_record({
            "schema_version": 1, "run_id": run_id, "mode": "run", "parent_run_id": "",
            "parent_context": {}, "thread_id": "", "created_at": pending.get("created_at", ""),
            "finished_at": now_iso(), "duration_seconds": 0, "queue_seconds": 0,
            "repo": pending["repo"], "task_mode": pending["manifest"].get("task_mode", "implement"),
            "orchestrator_session": pending.get("orchestrator_session", ""),
            "model": pending["model"], "effort": pending["effort"], "sandbox": "",
            "approval_policy": config["worker"]["approval_policy"],
            "ignore_user_config": pending.get("ignore_user_config"),
            "codex_cli_version": "", "prompt_version": 2,
            "manifest_sha256": manifest_hash(pending["manifest"]), "manifest": pending["manifest"],
            "resume_instruction": "", "git_before": {}, "git_after": {},
            "invocation_status": "failed", "worker_status": "unknown", "exit_code": None,
            "usage": {}, "event_counts": {}, "result": None, "error": str(exc),
        })
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def wait_for(run_ids, timeout, poll_seconds=2.0):
    """기록 파일이 생길 때까지 이 프로세스가 대기한다. Claude는 폴링하지 않는다."""
    for run_id in run_ids:
        record_path(run_id)
    deadline = time.monotonic() + max(0, timeout)
    while True:
        finished = {}
        pending = []
        for run_id in run_ids:
            path = record_path(run_id)
            if os.path.exists(path):
                finished[run_id] = public_result(load_record(run_id), path)
            elif os.path.exists(pending_path(run_id)):
                pending.append(run_id)
            else:
                raise WorkerError("알 수 없는 run ID: %s" % run_id)
        if not pending:
            results = [finished[run_id] for run_id in run_ids]
            return results[0] if len(results) == 1 else {"status": "completed", "runs": results}
        if time.monotonic() >= deadline:
            return {
                "status": "running",
                "pending": pending,
                "finished": [finished[r] for r in run_ids if r in finished],
                "wait_command": wait_command(pending, timeout),
            }
        time.sleep(min(poll_seconds, max(0.1, deadline - time.monotonic())))


def list_records(limit):
    ensure_runs_dir()
    rows = []
    for name in sorted(os.listdir(RUNS_DIR), reverse=True):
        if not name.endswith(".json") or not RUN_ID_RE.match(name[:-5]):
            continue
        try:
            record = read_json(os.path.join(RUNS_DIR, name), "실행 이력")
        except WorkerError:
            continue
        invocation = record.get("invocation_status", "unknown")
        status = (record.get("worker_status", "unknown")
                  if invocation == "completed" else invocation)
        rows.append({
            "run_id": record.get("run_id", name[:-5]),
            "created_at": record.get("created_at", ""),
            "mode": record.get("mode", ""),
            "parent_run_id": record.get("parent_run_id", ""),
            "repo": record.get("repo", ""),
            "model": record.get("model", ""),
            "effort": record.get("effort", ""),
            "task_mode": record.get("task_mode", "implement"),
            "status": status,
            "usage": record.get("usage", {}),
        })
        if len(rows) >= limit:
            break
    return rows


def add_execution_args(parser, include_repo=False):
    if include_repo:
        parser.add_argument("--repo", required=True)
    parser.add_argument("--model")
    parser.add_argument("--effort", choices=sorted(EFFORTS))
    parser.add_argument("--timeout", type=int, default=None,
                        help="Codex 실행 상한(초). 기본은 models.json routing.worker_timeout_seconds")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--orchestrator-session", default="")
    parser.add_argument(
        "--with-user-config", action="store_true",
        help="Codex 사용자 config·플러그인·MCP가 필요한 작업에서만 사용")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="새 전용 Codex 세션에서 작업 계약 실행")
    add_execution_args(run, include_repo=True)
    run.add_argument("--manifest", required=True, help="작업 계약 JSON 경로 또는 stdin은 -")
    run.add_argument("--detach", action="store_true",
                     help="분리 프로세스로 실행하고 run_id를 즉시 반환. 결과는 wait로 회수")

    wait = sub.add_parser("wait", help="detach 실행의 완료 기록을 blocking 대기")
    wait.add_argument("run_ids", nargs="+")
    wait.add_argument("--timeout", type=int, default=None,
                      help="대기 상한(초). 기본은 routing.wait_timeout_seconds")

    sub.add_parser("_exec-pending", help=argparse.SUPPRESS).add_argument("run_id")

    resume = sub.add_parser("resume", help="기존 Codex 스레드에서 후속 지시 실행")
    add_execution_args(resume)
    resume.add_argument("run_id")
    resume.add_argument("--instruction", required=True)

    rerun = sub.add_parser("rerun", help="저장된 작업 계약을 새 세션에서 다시 실행")
    add_execution_args(rerun)
    rerun.add_argument("run_id")

    listing = sub.add_parser("list", help="최근 실행 이력 요약")
    listing.add_argument("--limit", type=int, default=10)

    show = sub.add_parser("show", help="실행 이력 상세 JSON")
    show.add_argument("run_id")

    sub.add_parser("config", help="현재 모델 라우팅 설정")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            output = {"runs": list_records(max(1, args.limit))}
        elif args.command == "show":
            output = load_record(args.run_id)
        elif args.command == "config":
            output = load_config()
        elif args.command == "_exec-pending":
            exec_pending(args.run_id)
            return 0
        elif args.command == "wait":
            config = load_config()
            timeout = (args.timeout if args.timeout is not None
                       else config["routing"]["wait_timeout_seconds"])
            output = wait_for(args.run_ids, timeout)
        else:
            config = load_config()
            worker = config["worker"]
            timeout = (args.timeout if args.timeout is not None
                       else config["routing"]["worker_timeout_seconds"])
            if args.command == "run":
                manifest = load_manifest(
                    args.manifest,
                    default_effort=worker["default_effort"],
                    inspection_effort=worker["inspection_effort"],
                    max_contract_chars=(config["routing"]["max_contract_chars"]
                                        if args.orchestrator_session else 0))
                repo = resolve_repo(args.repo)
                model = args.model or worker["model"]
                effort = args.effort or manifest["worker_effort"] or worker["default_effort"]
                ignore_user_config = (False if args.with_user_config else
                                      worker["ignore_user_config"])
                if args.detach and not args.dry_run:
                    output = start_detached(repo, manifest, model, effort, config, timeout,
                                            ignore_user_config, args.orchestrator_session)
                else:
                    output = execute("run", repo, manifest, model, effort, config, timeout,
                                     dry_run=args.dry_run,
                                     ignore_user_config=ignore_user_config,
                                     orchestrator_session=args.orchestrator_session)
            else:
                parent = load_record(args.run_id)
                repo = resolve_repo(parent["repo"])
                manifest = normalize_manifest(parent["manifest"], worker["default_effort"])
                if args.command == "resume":
                    if not parent.get("thread_id"):
                        raise WorkerError("기존 실행에 thread_id가 없어 resume할 수 없음")
                    model = args.model or parent.get("model") or worker["model"]
                    effort = args.effort or parent.get("effort") or worker["default_effort"]
                    ignore_user_config = (False if args.with_user_config else
                                          parent.get("ignore_user_config",
                                                     worker["ignore_user_config"]))
                    output = execute(
                        "resume", repo, manifest, model, effort, config, timeout,
                        parent_run_id=args.run_id, thread_id=parent["thread_id"],
                        instruction=args.instruction, dry_run=args.dry_run,
                        parent_record=parent, ignore_user_config=ignore_user_config,
                        orchestrator_session=args.orchestrator_session)
                else:
                    model = args.model or worker["model"]
                    effort = args.effort or worker["default_effort"]
                    manifest["worker_effort"] = effort
                    output = execute(
                        "rerun", repo, manifest, model, effort, config, timeout,
                        parent_run_id=args.run_id, dry_run=args.dry_run,
                        parent_record=parent,
                        ignore_user_config=(False if args.with_user_config else
                                            worker["ignore_user_config"]),
                        orchestrator_session=args.orchestrator_session)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except WorkerError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
