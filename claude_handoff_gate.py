#!/usr/bin/env python3
"""Claude-only hard handoff state machine.

UserPromptSubmit classifies without an LLM and injects a route-specific compact contract.
PreToolUse prevents a delegated task from turning back into Claude-led exploration/editing.
The worker protocol is `run --detach` (returns run_id immediately) followed by bounded
`wait` calls that block inside the worker process, so Claude never polls and the Bash tool
timeout cannot kill a long Codex run. PostToolUse tracks run ids and marks the result ready;
Stop enforces one handoff attempt. Prompt text is never persisted.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time

import codex_worker
import edit_detect
import harness_lib as lib

STATE_DIR = os.path.join(lib.CACHE_DIR, "claude-handoff")
MUTATION_RE = re.compile(
    r"(구현|개선|수정|고쳐|고치|추가|변경|적용|업데이트|리팩터|작성|만들|삭제|제거|"
    r"문서화|고도화|마이그레이|진행해|implement|\bfix\b|\badd\b|\bupdate\b|"
    r"refactor|remove|create|migrate)", re.IGNORECASE)
FOLLOWUP_ACTION_RE = re.compile(
    r"^\s*(?:응[, ]*)?(?:(?:그렇게|그대로)\s*)?(?:해줘|진행해줘|진행하자|하자)\s*[.!?]*\s*$",
    re.IGNORECASE)
STRONG_ACTION_RE = re.compile(
    r"(구현해|개선해|수정해|고쳐|추가해|변경해|적용해|업데이트해|리팩터링해|작성해|"
    r"만들어|삭제해|제거해|문서화해|마이그레이션해|진행해|implement\b|fix\b|add\b|"
    r"update\b|refactor\b|remove\b|create\b|migrate\b)", re.IGNORECASE)
IMPERATIVE_RE = re.compile(r"(해\s*줘|해\s*줄래|해\s*주세요|해\s*주실|해\s*주라|해라|하자|"
                           r"please\b|can you\b|could you\b)", re.IGNORECASE)
QUESTION_RE = re.compile(r"(\?|맞아|어때|설명해|알려|추천해|괜찮아|should\b|what\b|why\b)",
                         re.IGNORECASE)
INSPECT_RE = re.compile(
    r"(분석|진단|원인|리뷰|검토|조사|확인|찾아|왜|실행|돌려|analy[sz]e|diagnos|review|inspect|"
    r"investigat|root cause)", re.IGNORECASE)
CODE_RE = re.compile(
    r"(코드|레포|저장소|파일|버그|테스트|빌드|함수|클래스|모듈|api|db|sql|git|"
    r"프론트|백엔드|서버|컴포넌트|\.py\b|\.tsx?\b|\.jsx?\b|\.go\b|\.rs\b|"
    r"code|repo|repository|test|build|function|module|component)", re.IGNORECASE)
ARCHITECT_RE = re.compile(
    r"(아키텍처|구조 설계|설계 방향|어떻게 설계|데이터 모델|권한 모델|인증 구조|"
    r"결제 구조|architecture|design decision|data model)", re.IGNORECASE)
BATCH_RE = re.compile(
    r"(여러\s*(?:레포|저장소|프로젝트)|모든\s*(?:레포|저장소|프로젝트)|전체\s*레포|"
    r"각\s*(?:레포|저장소|프로젝트)|multiple repositories|all repos)", re.IGNORECASE)
WORKER_RE = re.compile(r"codex_worker\.py\s+(run|resume|wait)\b")
RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
FILE_REF_RE = re.compile(r"[\w./-]+\.(?:py|tsx?|jsx?|go|rs|md|json|ya?ml|toml|sql|css|html)\b")
SHORT_PROMPT_CHARS = 80
WORKER_SCRIPT = "~/.claude/hooks/workspace-harness/codex_worker.py"
SINGLE_BACKGROUND_RE = re.compile(r"(?<!&)&(?!&)(?:\s*$|\s*[;])")
OUTPUT_REDIRECT_RE = re.compile(r"(?<![<>])>(?!>)")
SAFE_GIT_ACTIONS = {
    ("rev-parse", "--show-toplevel"),
    ("branch", "--show-current"),
    ("status", "--short"),
    ("status", "--short", "--branch"),
    ("worktree", "list"),
}


def session_key(data):
    raw = data.get("session_id") or data.get("conversation_id") or ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20] if raw else ""


def _state_path(key):
    return os.path.join(STATE_DIR, key + ".json")


@contextlib.contextmanager
def locked_state(key):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    lock_path = os.path.join(STATE_DIR, key + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.utime(lock_path, None)
        state = load_state(key)
        yield state
        save_state(key, state)
    finally:
        os.close(fd)


def load_state(key):
    try:
        with open(_state_path(key), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(key, state):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".%s-" % key, dir=STATE_DIR, text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
            f.write("\n")
        os.replace(temp_path, _state_path(key))
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def classify_prompt(prompt, config, previous=None):
    """previous는 직전 턴 상태(route·reason). 후속 문구는 직전 explicit 선택을 상속한다."""
    text = (prompt or "").strip()
    lower = text.lower()
    route = config["routing"]
    if any(phrase.lower() in lower for phrase in route["bypass_phrases"]):
        return "direct", "explicit-opt-out"
    if route["forced_command"].lower() in lower:
        return "implement", "explicit-force"
    if ARCHITECT_RE.search(text):
        return "architect", "architecture-decision"
    if FOLLOWUP_ACTION_RE.match(text):
        prev_reason = (previous or {}).get("reason", "")
        if prev_reason in ("explicit-opt-out", "follow-up-inherits-opt-out"):
            # "응 그렇게 해줘"는 직전에 "직접 해줘"로 시작한 작업의 연속이다.
            return "direct", "follow-up-inherits-opt-out"
        return "implement", "action-follow-up"
    if text.endswith("?") and not IMPERATIVE_RE.search(text):
        # "…진행해?"처럼 행동어가 들어간 물음표 문장은 요청이 아니라 질문이다.
        return "direct", "question-mark"
    mutation = bool(MUTATION_RE.search(text))
    inspection = bool(INSPECT_RE.search(text))
    code = bool(CODE_RE.search(text))
    strong_action = bool(STRONG_ACTION_RE.search(text))
    if BATCH_RE.search(text) and (strong_action or (inspection and code)):
        return "batch", "multi-repository-task"
    question = bool(QUESTION_RE.search(text))
    if not strong_action and question and len(text) <= SHORT_PROMPT_CHARS:
        # 한 줄 질문은 Codex inspect(수만 토큰·수 분)보다 Claude가 바로 답하는 편이 싸다.
        return "direct", "short-question"
    if not strong_action and len(FILE_REF_RE.findall(text)) == 1 and len(text) <= 160:
        # 파일 하나를 콕 집은 짧은 요청은 Claude가 그 파일만 읽고 답한다.
        return "direct", "single-file-reference"
    if inspection and code and not strong_action:
        return "inspect", "repository-investigation"
    if question and not strong_action:
        return "direct", "question-or-decision"
    if mutation:
        return "implement", "code-or-artifact-change"
    return "direct", "conversation-or-decision"


def route_context(route, state, config):
    routing = config["routing"]
    worker = config["worker"]
    session = state["session"]
    wait_timeout = routing["wait_timeout_seconds"]
    common = (
        "Protocol: `run ... --detach` returns run_id at once; then call "
        "`python3 {script} wait <run_id> --timeout {wait}` (blocking, foreground, no "
        "run_in_background, no `&`, no redirect) and repeat that exact wait command while it "
        "reports status=running (at most {max_wait} waits). Never poll with other tools or reread "
        "raw logs. When wait returns the final JSON, answer from it without another tool."
    ).format(script=WORKER_SCRIPT, wait=wait_timeout, max_wait=routing["max_wait_calls"])
    if route == "direct":
        return "[HARNESS ROUTE=CLAUDE_DIRECT] Handle this request directly; no Codex handoff is required."
    if route == "architect":
        return (
            "[HARNESS ROUTE=FABLE_ARCHITECT_THEN_CODEX] Use fable-architect at most once and do no "
            "main-session repository exploration. Then create one compact manifest (max {chars} chars) "
            "and start one worker: `python3 {script} "
            "run --repo <known-git-root> --orchestrator-session {session} --manifest - --detach`. Planner effort "
            "is {planner}; the architect is only for this high-risk decision. {common}"
        ).format(script=WORKER_SCRIPT, chars=routing["max_contract_chars"],
                 session=session, planner=config["planner"]["default_effort"], common=common)
    if route == "batch":
        return (
            "[HARNESS ROUTE=CODEX_BATCH] Do no repository exploration or editing in Claude. Create at "
            "most {workers} independent manifests, each <= {chars} characters, and issue their "
            "`codex_worker.py run --repo <known-git-root> --orchestrator-session {session} --manifest - "
            "--detach` calls together in one parallel tool batch, then a single `wait <id> <id> ...` "
            "for all run ids. Do not start further runs after any result. {common}"
        ).format(workers=routing["max_batch_worker_calls"], chars=routing["max_contract_chars"],
                 session=session, common=common)
    task_mode = "inspect" if route == "inspect" else "implement"
    effort = worker["inspection_effort"] if route == "inspect" else worker["default_effort"]
    return (
        "[HARNESS ROUTE=CODEX_{label}] Do not inspect or edit the repository in Claude. From the user "
        "request, create one manifest no longer than {chars} characters with task_mode={mode}, "
        "task_class, objective, worker_effort={effort}, scope, acceptance_criteria, constraints, and "
        "verification_commands. Start exactly once via stdin heredoc: `python3 {script} "
        "run --repo <known-git-root> --orchestrator-session {session} "
        "--manifest - --detach <<'JSON' ... JSON`. {common}"
    ).format(label=route.upper(), script=WORKER_SCRIPT, chars=routing["max_contract_chars"],
             mode=task_mode, effort=effort, session=session, common=common)


def _segments(command):
    parts = []
    current = []
    for token in edit_detect.tokens(command.replace("\n", ";")):
        if token in ("&&", "||", ";", "|"):
            if current:
                parts.append(current)
                current = []
        else:
            current.append(token)
    if current:
        parts.append(current)
    return parts


def is_safe_git_command(command):
    segments = _segments(command)
    if not segments:
        return False
    for args in segments:
        if args == ["pwd"]:
            continue
        if not args or os.path.basename(args[0]) != "git":
            return False
        args = args[1:]
        if len(args) >= 2 and args[0] == "-C":
            args = args[2:]
        if tuple(args) not in SAFE_GIT_ACTIONS:
            return False
    return True


def worker_action(command):
    match = WORKER_RE.search(command or "")
    return match.group(1) if match else ""


def _clean_shell_prefix(command, prefix_tokens):
    if any(token in ("&", "&&", "|", "||", ";", ">", ">>", "<") for token in prefix_tokens):
        return False
    if SINGLE_BACKGROUND_RE.search(command) or OUTPUT_REDIRECT_RE.search(command):
        return False
    return True


def valid_wait_command(command):
    """`codex_worker.py wait <run_id>... [--timeout N]` 형식만 허용한다."""
    if command.count("codex_worker.py") != 1:
        return False
    tokens = edit_detect.tokens(command)
    if not tokens or not _clean_shell_prefix(command, tokens):
        return False
    try:
        worker_index = next(i for i, token in enumerate(tokens)
                            if os.path.basename(token) == "codex_worker.py")
    except StopIteration:
        return False
    rest = tokens[worker_index + 1:]
    if not rest or rest[0] != "wait":
        return False
    rest = rest[1:]
    if "--timeout" in rest:
        idx = rest.index("--timeout")
        if idx + 1 >= len(rest) or not rest[idx + 1].isdigit():
            return False
        rest = rest[:idx] + rest[idx + 2:]
    return bool(rest) and all(RUN_ID_RE.match(token) for token in rest)


def valid_worker_command(command, action, orchestrator_session):
    """hard route의 `run --detach` 형식만 허용한다."""
    if action == "wait":
        return valid_wait_command(command)
    if action != "run" or command.count("codex_worker.py") != 1:
        return False
    tokens = edit_detect.tokens(command)
    if not tokens or "<<" not in tokens:
        return False
    heredoc = tokens.index("<<")
    prefix = tokens[:heredoc]
    if any(token in ("&", "&&", "|", "||", ";", ">", ">>", "<")
           for token in prefix):
        return False
    shell_prefix = command.split("<<", 1)[0]
    if SINGLE_BACKGROUND_RE.search(shell_prefix) or OUTPUT_REDIRECT_RE.search(shell_prefix):
        return False
    try:
        worker_index = next(i for i, token in enumerate(prefix)
                            if os.path.basename(token) == "codex_worker.py")
        session_index = prefix.index("--orchestrator-session")
        manifest_index = prefix.index("--manifest")
    except (StopIteration, ValueError):
        return False
    if worker_index + 1 >= len(prefix) or prefix[worker_index + 1] != "run":
        return False
    if session_index + 1 >= len(prefix) or prefix[session_index + 1] != orchestrator_session:
        return False
    if manifest_index + 1 >= len(prefix) or prefix[manifest_index + 1] != "-":
        return False
    return "--detach" in prefix


def run_template(session):
    return (
        "허용 형식: `python3 %s run --repo <git-root> --orchestrator-session %s --manifest - "
        "--detach <<'JSON' {...} JSON` 그리고 `python3 %s wait <run_id> --timeout N` "
        "(foreground, run_in_background 없이)." % (WORKER_SCRIPT, session or "<session>",
                                                   WORKER_SCRIPT))


def deny(reason, hard=False):
    if hard:
        return {"continue": False, "stopReason": reason}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def pre_tool(state, tool_name, tool_input, config):
    route = state.get("route", "direct")
    if route == "direct":
        return None
    phase = state.get("phase", "planning")
    routing = config["routing"]
    session = state.get("session", "")
    command = (tool_input or {}).get("command", "") if tool_name == "Bash" else ""
    action = worker_action(command)
    if tool_name == "Bash" and (tool_input or {}).get("run_in_background"):
        # 명령 문자열의 `&`뿐 아니라 도구 파라미터로 background를 켜는 우회도 막는다.
        state["background_attempts"] = state.get("background_attempts", 0) + 1
        return _violation(state, config,
                          "run_in_background 금지. worker는 `run --detach`로 띄우고 `wait`로 "
                          "foreground에서 기다리세요. " + run_template(session))
    if tool_name == "Bash" and action:
        if not valid_worker_command(command, action, session):
            return _violation(state, config,
                              "Codex 호출 형식이 다릅니다. " + run_template(session))
        if action == "wait":
            if phase == "result_ready":
                return deny("Codex 결과가 이미 도착했습니다. 추가 wait 없이 구조화 결과만 요약하세요.",
                            hard=True)
            if phase != "delegated":
                return _violation(state, config,
                                  "wait는 `run --detach` 뒤에만 호출합니다. " + run_template(session))
            if state.get("wait_calls", 0) >= routing["max_wait_calls"]:
                return deny("wait 호출 상한(%d회)에 도달했습니다. 현재까지의 상태를 run ID와 함께 "
                            "보고하고 종료하세요." % routing["max_wait_calls"], hard=True)
            state["wait_calls"] = state.get("wait_calls", 0) + 1
            return None
        max_runs = routing["max_batch_worker_calls"] if route == "batch" else 1
        can_add_batch = route == "batch" and phase == "delegated" and not state.get("wait_calls")
        if (action == "run" and (phase == "planning" or can_add_batch)
                and state.get("worker_calls", 0) < max_runs):
            state["phase"] = "delegated"
            state["worker_calls"] = state.get("worker_calls", 0) + 1
            return None
        return _violation(state, config, "허용된 Codex run 횟수 또는 순서를 초과했습니다. "
                          "실행 중인 run은 `wait`로 회수하세요.")
    if phase == "result_ready":
        return deny("Codex 결과가 이미 도착했습니다. 추가 도구 없이 구조화 결과만 요약하세요.",
                    hard=True)
    if phase == "delegated":
        return _violation(state, config,
                          "Codex worker 실행 중에는 다른 도구를 쓰지 않습니다. "
                          "`wait <run_id>`로 결과를 기다리세요. " + run_template(session))
    if tool_name == "Bash" and is_safe_git_command(command):
        if state.get("planner_tool_calls", 0) < config["routing"]["max_planner_tool_calls"]:
            state["planner_tool_calls"] = state.get("planner_tool_calls", 0) + 1
            return None
    if tool_name in ("Agent", "Task") and route == "architect":
        description = json.dumps(tool_input or {}, ensure_ascii=False)
        if "fable-architect" in description and not state.get("architect_calls"):
            state["architect_calls"] = 1
            return None
    return _violation(
        state, config,
        "이 요청은 Codex로 라우팅됐습니다. Claude 탐색·편집·일반 Bash 대신 compact manifest를 "
        "넘기세요. " + run_template(session))


def _violation(state, config, reason):
    state["violations"] = state.get("violations", 0) + 1
    hard = state["violations"] > config["routing"]["max_route_violations"]
    return deny(reason, hard=hard)


def _tool_response_text(response):
    if isinstance(response, str):
        return response
    if not isinstance(response, dict):
        return ""
    return " ".join(str(response.get(key) or "") for key in
                    ("stdout", "stderr", "output", "content", "text"))


def post_tool(state, tool_name, tool_input, tool_response):
    if tool_name != "Bash":
        return
    action = worker_action((tool_input or {}).get("command", ""))
    if not action:
        return
    text = _tool_response_text(tool_response)
    matches = re.findall(r'"run_id"\s*:\s*"([0-9]{8}-[0-9]{6}-[0-9a-f]{6})"', text)
    known = state.setdefault("run_ids", [])
    for run_id in matches:
        if run_id not in known:
            known.append(run_id)
    still_running = bool(re.search(r'"status"\s*:\s*"running"', text))
    if action == "run" and still_running:
        return  # detach 성공: wait로 회수할 때까지 delegated 유지
    if action == "wait" and still_running:
        return  # wait 시간 초과: 같은 wait를 다시 호출한다
    if action == "wait":
        state["completed_calls"] = state.get("worker_calls", 1)
    else:
        state["completed_calls"] = state.get("completed_calls", 0) + 1
    state["phase"] = ("result_ready" if state["completed_calls"] >= state.get("worker_calls", 1)
                      else "delegated")


def stop_decision(state):
    if state.get("route") == "direct" or state.get("phase") == "result_ready":
        return None
    if state.get("stop_blocks", 0):
        return None
    state["stop_blocks"] = 1
    return {
        "decision": "block",
        "reason": "이 요청은 Codex handoff 대상입니다. 저장소를 직접 다루지 말고 compact manifest로 worker를 한 번 호출하세요.",
    }


def handle(data, config=None):
    if not lib.in_workspace(data.get("cwd", "")):
        return None
    key = session_key(data)
    if not key:
        return None
    config = config or codex_worker.load_config()
    event = data.get("hook_event_name", "")
    with locked_state(key) as state:
        if event == "UserPromptSubmit":
            previous = {"route": state.get("route", ""), "reason": state.get("reason", "")}
            route, reason = classify_prompt(data.get("prompt", ""), config, previous)
            state.clear()
            state.update({
                "session": key[:12],
                "route": route,
                "reason": reason,
                "phase": "planning",
                "planner_tool_calls": 0,
                "worker_calls": 0,
                "wait_calls": 0,
                "background_attempts": 0,
                "violations": 0,
                "stop_blocks": 0,
                "updated_at": time.time(),
            })
            lib.event("route-decision", key, "%s:%s" % (route, reason))
            return json.loads(lib.hook_output(event, route_context(route, state, config)))
        if event == "PreToolUse":
            decision = pre_tool(state, data.get("tool_name", ""), data.get("tool_input"), config)
            if decision:
                kind = "background" if (data.get("tool_input") or {}).get(
                    "run_in_background") else data.get("tool_name", "")
                lib.event("route-block", key, "%s:%s" % (state.get("route", ""), kind))
            return decision
        if event in ("PostToolUse", "PostToolUseFailure"):
            post_tool(state, data.get("tool_name", ""), data.get("tool_input"),
                      data.get("tool_response") or data.get("error"))
            return None
        if event == "Stop":
            decision = stop_decision(state)
            if decision:
                lib.event("route-stop-block", key, state.get("route", ""))
            elif state.get("run_ids"):
                lib.event("route-complete", key, ",".join(state["run_ids"][:4]))
            return decision
    return None


def main():
    data = lib.read_hook_input()
    decision = handle(data)
    if decision:
        print(json.dumps(decision, ensure_ascii=False))
    lib.prune_old_files(STATE_DIR, lib.STATE_TTL_SECONDS)


if __name__ == "__main__":
    lib.run_fail_open(main)
