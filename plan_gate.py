#!/usr/bin/env python3
"""PreToolUse(Edit|Write|NotebookEdit): 계획 마커 없는 소스 편집을 차단."""
import json
import os

import harness_lib as lib

ALLOWED_SEGMENTS = ("/docs/", "/.claude/")
ALLOWED_PREFIXES = (
    "/private/tmp/claude-",
    "/tmp/claude-",
    os.path.expanduser("~/.claude") + os.sep,
)

DENY_MESSAGE = (
    "[workspace harness] 이 워크스페이스는 계획 우선입니다. 소스 편집 전에 다음 중 하나를 하세요: "
    "(1) 플랜 모드로 계획을 세우고 승인받기, "
    "(2) superpowers writing-plans 스킬로 계획 문서를 docs/superpowers/plans/ 또는 docs/plans/에 작성하기 "
    "— 설계가 무거운 작업(다중 파일·아키텍처 결정·까다로운 버그)이면 deep-plan 스킬로 "
    "상위 모델 서브에이전트의 계획 생성·검토를 받으세요, "
    "(3) 한두 줄짜리 사소한 수정이라면 사용자에게 '!quick'을 포함한 메시지로 게이트 해제를 요청하기. "
    "계획 문서(15줄 이상, 검증 계획 포함)를 작성하면 이후 편집이 허용됩니다."
)


def target_path(tool_input):
    return tool_input.get("file_path") or tool_input.get("notebook_path") or ""


def is_allowed_path(path):
    if not path:
        return False
    real = os.path.realpath(path)
    if os.path.basename(real) in ("CLAUDE.md", "AGENTS.md"):
        return True
    for prefix in ALLOWED_PREFIXES:
        if real.startswith(prefix):
            return True
    for segment in ALLOWED_SEGMENTS:
        if segment in real:
            return True
    return False


def decide(data):
    """차단이면 deny JSON dict, 허용이면 None."""
    if not lib.in_workspace(data.get("cwd", "")):
        return None
    if lib.has_marker(data.get("session_id", "")):
        return None
    if data.get("permission_mode") == "plan":
        return None  # 플랜 모드 중 편집 제어는 Claude Code 기본 동작에 맡김
    if is_allowed_path(target_path(data.get("tool_input") or {})):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": DENY_MESSAGE,
        }
    }


def main():
    data = lib.read_hook_input()
    decision = decide(data)
    if decision:
        lib.event("plan-deny", data.get("session_id", ""),
                  target_path(data.get("tool_input") or {}))
        print(json.dumps(decision, ensure_ascii=False))


if __name__ == "__main__":
    lib.run_fail_open(main)
