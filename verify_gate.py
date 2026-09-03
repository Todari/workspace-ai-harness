#!/usr/bin/env python3
"""편집-후-검증 게이트.

PostToolUse: Claude/Codex의 소스 편집 시각과 검증 명령 실행 시각을 기록.
Stop: 마지막 소스 편집 이후 검증이 없으면 편집 세대당 1회만 검증을 지시한다.
"""
import json
import os
import re
import time

import edit_detect
import harness_lib as lib

VERIFY_DIR = os.path.join(lib.CACHE_DIR, "verify-gate")
PATH_EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")
PATCH_EDIT_TOOLS = ("apply_patch", "ApplyPatch")
SHELL_TOOLS = ("Bash", "exec_command", "shell", "unified_exec")
PACKAGE_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
PACKAGE_RUNNERS = {"npx", "bunx"}
VERIFY_SCRIPT_RE = re.compile(
    r"^(?:test|build|lint|type-?check|typecheck|check|verify|ci)(?::|$)", re.IGNORECASE)
DIRECT_VERIFY_TOOLS = {
    "tsc", "vitest", "jest", "playwright", "pytest", "py.test", "mypy",
    "eslint", "biome", "golangci-lint",
}
OPTIONS_WITH_VALUES = {
    "--filter", "-F", "--dir", "-C", "--workspace", "-w", "--prefix",
    "--cwd", "--project", "-p", "--config",
}
NON_SOURCE_DOC_NAMES = {
    "agents.md", "claude.md", "readme.md", "changelog.md", "license",
    "license.md", "contributing.md",
}
NON_SOURCE_DOC_SUFFIXES = (".md", ".txt", ".rst", ".adoc")
VERIFY_MESSAGE = (
    "[workspace harness] 소스 변경 뒤 검증이 없습니다. 레포 요약의 완료 기준 명령을 우선하고, "
    "없으면 변경과 가장 관련 있는 typecheck·test·build를 실행해 결과를 확인한 뒤 종료하세요."
)


def _state_path(session_id):
    return os.path.join(VERIFY_DIR, session_id.replace(os.sep, "_") + ".json")


def load_state(session_id):
    try:
        with open(_state_path(session_id), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"last_edit": 0, "last_verify": 0, "last_prompted_edit": 0}


def record(session_id, key):
    if not session_id:
        return
    os.makedirs(VERIFY_DIR, exist_ok=True)
    state = load_state(session_id)
    state[key] = time.time()
    with open(_state_path(session_id), "w", encoding="utf-8") as f:
        json.dump(state, f)
    lib.prune_old_files(VERIFY_DIR, lib.STATE_TTL_SECONDS)


def is_verify_command(command):
    return any(_is_verify_argv(segment) for segment in _command_segments(command))


def _command_segments(command):
    """명령을 실행 단위로 쪼갠다.

    줄바꿈도 `;`와 같은 구분자다. shlex는 줄바꿈을 공백으로 흘려보내므로 먼저 줄 단위로
    나눠야 여러 줄 스크립트의 두 번째 줄(`cd foo` 다음의 실제 검증 명령)을 놓치지 않는다.
    heredoc 본문은 셸 구문이 아니라 하위 프로세스 입력이므로 건너뛰되, 종료 구분자
    다음 줄부터는 다시 셸 명령이므로 파싱을 재개한다.
    """
    segments = []
    lines = command.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        current = []
        for token in edit_detect.tokens(line):
            if token in ("|", "||", "&&", ";"):
                if current:
                    segments.append(current)
                    current = []
            else:
                current.append(token)
        if current:
            segments.append(current)

        delimiter = edit_detect.heredoc_delimiter(line)
        if delimiter is not None:
            i += 1
            while i < len(lines) and lines[i].strip() != delimiter:
                i += 1
        i += 1
    return segments




def _without_options(args):
    remaining = list(args)
    while remaining and remaining[0].startswith("-"):
        option = remaining.pop(0)
        if "=" not in option and option in OPTIONS_WITH_VALUES and remaining:
            remaining.pop(0)
    return remaining


def _without_prefixes(argv):
    args = list(argv)
    if args and args[0] == "env":
        args.pop(0)
        args = _without_options(args)
    while args and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", args[0]):
        args.pop(0)
    while args and os.path.basename(args[0]) in ("command", "time", "sudo"):
        args.pop(0)
        args = _without_options(args)
    return args


def _is_verify_argv(argv):
    args = _without_prefixes(argv)
    if not args:
        return False
    tool = os.path.basename(args[0]).lower()
    rest = args[1:]

    if tool in PACKAGE_MANAGERS:
        rest = _without_options(rest)
        if not rest:
            return False
        action = rest[0].lower()
        if action == "run" and len(rest) > 1:
            return bool(VERIFY_SCRIPT_RE.match(rest[1]))
        if VERIFY_SCRIPT_RE.match(action):
            return True
        if action in DIRECT_VERIFY_TOOLS:
            return True
        if action in ("exec", "dlx", "x"):
            return _is_verify_argv(_without_options(rest[1:]))
        return False

    if tool in PACKAGE_RUNNERS:
        return _is_verify_argv(_without_options(rest))
    if tool == "turbo":
        rest = _without_options(rest)
        if rest and rest[0] == "run":
            rest = rest[1:]
        return bool(rest) and bool(VERIFY_SCRIPT_RE.match(rest[0]))
    if tool in DIRECT_VERIFY_TOOLS:
        return True
    if tool.startswith("python"):
        return any(rest[i:i + 2] and rest[i] == "-m"
                   and rest[i + 1] in ("pytest", "unittest", "compileall")
                   for i in range(len(rest) - 1))
    if tool == "go":
        return bool(rest) and rest[0] in ("test", "vet", "build")
    if tool == "cargo":
        rest = _without_options(rest)
        return bool(rest) and rest[0] in ("test", "check", "clippy", "build")
    if tool in ("gradle", "gradlew") or tool.endswith("gradlew"):
        return any(VERIFY_SCRIPT_RE.match(part.rsplit(":", 1)[-1]) for part in rest)
    if tool in ("mvn", "mvnw") or tool.endswith("mvnw"):
        return any(part in ("test", "verify", "package") for part in rest)
    if tool in ("flutter", "dart"):
        return bool(rest) and rest[0] in ("test", "analyze", "build")
    if tool in ("swift", "dotnet", "mix", "make", "rake"):
        return any(VERIFY_SCRIPT_RE.match(part.rsplit(":", 1)[-1]) for part in rest)
    if tool in ("next", "vite"):
        return bool(rest) and rest[0] == "build"
    if tool == "ruff":
        return bool(rest) and rest[0] == "check"
    if tool == "prettier":
        return "--check" in rest
    return False


# Claude와 Codex 모두 성공한 도구 호출에 PostToolUse를 발화한다. 이 마커들은
# `tsc || true` 류 합성 명령(전체 exit 0)의 실패 출력을 걸러내는 보조선이다.
FAILURE_MARKERS = ("error TS", "npm ERR!", "Traceback (most recent call last)",
                   "FAILED", "FAIL ", "ELIFECYCLE")


def is_successful_response(tool_response):
    if isinstance(tool_response, str):
        return not any(marker in tool_response for marker in FAILURE_MARKERS)
    if not isinstance(tool_response, dict):
        return True  # 스키마 판단 불가 — fail-open으로 성공 취급
    exit_code = tool_response.get("exit_code", tool_response.get("exitCode"))
    if exit_code is not None and exit_code != 0:
        return False
    if tool_response.get("interrupted"):
        return False
    text = "".join(str(tool_response.get(key) or "") for key in (
        "stdout", "stderr", "output", "aggregated_output", "content", "text"))
    return not any(marker in text for marker in FAILURE_MARKERS)


def command_from(tool_input):
    tool_input = tool_input or {}
    return tool_input.get("command") or tool_input.get("cmd") or ""


def patch_paths(patch):
    return re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch or "", re.MULTILINE)


def is_verification_relevant_path(path):
    if not path or not edit_detect.is_workspace_source(os.path.realpath(path)):
        return False
    name = os.path.basename(path).lower()
    return name not in NON_SOURCE_DOC_NAMES and not name.endswith(NON_SOURCE_DOC_SUFFIXES)


def is_source_edit(tool_name, tool_input, cwd=""):
    tool_input = tool_input or {}
    if tool_name in PATH_EDIT_TOOLS:
        path = edit_detect.target_path(tool_input)
        if path and not os.path.isabs(path):
            path = os.path.join(cwd, path)
        return is_verification_relevant_path(path)
    if tool_name in PATCH_EDIT_TOOLS:
        # Codex canonical schema uses `command`; Claude-compatible adapters may use patch/input.
        patch = (tool_input.get("patch") or tool_input.get("input")
                 or tool_input.get("command") or "")
        paths = patch_paths(patch)
        if not paths:
            return True  # 스키마를 모르면 편집으로 간주해 검증 누락만 안내
        absolute = [p if os.path.isabs(p) else os.path.join(cwd, p) for p in paths]
        return any(is_verification_relevant_path(p) for p in absolute)
    if tool_name in SHELL_TOOLS:
        return bool(edit_detect.find_write_target(command_from(tool_input), cwd))
    return False


def session_key(data):
    return (data.get("session_id") or data.get("thread_id")
            or data.get("conversation_id") or "")


def handle(data):
    """PostToolUse: 기록 후 None. Stop: 차단 필요 시 block dict, 아니면 None."""
    if not lib.in_workspace(data.get("cwd", "")):
        return None
    session_id = session_key(data)
    event = data.get("hook_event_name", "")
    if event == "PostToolUse":
        tool = data.get("tool_name", "")
        # 편집과 검증은 배타적이지 않다. `파일 수정 && 테스트` 한 줄이면 둘 다 기록해야
        # 검증이 묻히지 않는다. 편집을 먼저 기록해 같은 초에 들어와도 검증이 뒤에 온다.
        if is_source_edit(tool, data.get("tool_input"), data.get("cwd", "")):
            record(session_id, "last_edit")
        if tool in SHELL_TOOLS and is_verify_command(command_from(data.get("tool_input"))) \
                and is_successful_response(data.get("tool_response")):
            record(session_id, "last_verify")
        return None
    if event == "Stop":
        if data.get("stop_hook_active"):
            return None  # 이미 한 번 차단했음 — 무한 루프 방지
        state = load_state(session_id)
        if state.get("last_edit", 0) and \
                state.get("last_verify", 0) < state.get("last_edit", 0) and \
                state.get("last_prompted_edit", 0) < state.get("last_edit", 0):
            state["last_prompted_edit"] = state["last_edit"]
            os.makedirs(VERIFY_DIR, exist_ok=True)
            with open(_state_path(session_id), "w", encoding="utf-8") as f:
                json.dump(state, f)
            return {"decision": "block", "reason": VERIFY_MESSAGE}
    return None


def main():
    data = lib.read_hook_input()
    decision = handle(data)
    if decision:
        lib.event("verify-block", session_key(data))
        print(json.dumps(decision, ensure_ascii=False))


if __name__ == "__main__":
    lib.run_fail_open(main)
