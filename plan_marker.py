#!/usr/bin/env python3
"""PostToolUse(Edit|Write|ExitPlanMode): 계획 완료를 감지해 세션 마커 기록.

계획 문서 경로는 최소 품질(줄 수·검증 계획 포함)을 통과해야 마커가 활성화된다 —
한 줄짜리 형식적 계획으로 게이트를 통과하는 것을 막는다.
"""
import json
import os

import harness_lib as lib

PLAN_DIR_SEGMENTS = ("/docs/superpowers/plans/", "/docs/plans/")
MIN_PLAN_LINES = 15
VERIFY_KEYWORDS = ("검증", "테스트", "test", "verify", "확인")

SHALLOW_MESSAGE = (
    "[workspace harness] 계획 문서가 최소 기준 미달(%s)이라 편집 게이트가 아직 잠겨 있습니다. "
    "변경할 파일 목록, 단계, 검증 방법(어떤 명령으로 확인할지)을 포함해 계획을 보강하세요."
)
ARMED_MESSAGE = (
    "[workspace harness] 계획 마커 활성화 — 편집이 허용됩니다. 실행 전에 계획의 결함"
    "(누락된 파일, 잘못된 가정, 검증 공백)을 스스로 또는 서브에이전트로 한 번 비판 검토하면 좋습니다. "
    "설계가 무거우면 deep-plan 스킬로 상위 모델 검토를 받을 수 있습니다."
)


def plan_quality_issue(path):
    """계획 문서가 최소 기준 미달이면 사유 문자열, 충족(또는 판단 불가)이면 None."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None  # 읽기 실패 — fail-open
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < MIN_PLAN_LINES:
        return "%d줄 (최소 %d줄)" % (len(lines), MIN_PLAN_LINES)
    lowered = text.lower()
    if not any(k in lowered for k in VERIFY_KEYWORDS):
        return "검증 계획 없음"
    return None


def marker_reason(data):
    """마커를 기록해야 하면 사유 문자열, 아니면 None."""
    if not lib.in_workspace(data.get("cwd", "")):
        return None
    if data.get("tool_name") == "ExitPlanMode":
        return "plan-mode-approved"
    path = (data.get("tool_input") or {}).get("file_path") or ""
    if path:
        real = os.path.realpath(path)
        for segment in PLAN_DIR_SEGMENTS:
            if segment in real:
                return "plan-doc:" + real
    return None


def _context_out(message):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": message,
        }
    }, ensure_ascii=False))


def main():
    data = lib.read_hook_input()
    reason = marker_reason(data)
    if not reason:
        return
    session_id = data.get("session_id", "")
    if reason.startswith("plan-doc:"):
        issue = plan_quality_issue(reason[len("plan-doc:"):])
        if issue:
            lib.event("plan-shallow", session_id, issue)
            _context_out(SHALLOW_MESSAGE % issue)
            return
    already = lib.has_marker(session_id)
    lib.set_marker(session_id, reason)
    if not already:
        lib.event("plan-marker", session_id, reason[:80])
        if reason.startswith("plan-doc:"):
            _context_out(ARMED_MESSAGE)


if __name__ == "__main__":
    lib.run_fail_open(main)
