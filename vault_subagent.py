#!/usr/bin/env python3
"""SubagentStart: 서브에이전트에 볼트 읽기 인터페이스를 알린다 (쓰기는 금지).

메인 세션은 SessionStart 훅으로 볼트를 인지하지만 서브에이전트는 그 훅이 돌지 않는다.
서브에이전트가 볼트를 직접 grep하면 이 iCloud 경로에서 조용히 0건이 나오므로,
표준 검색 도구의 존재와 읽기 전용 규칙을 여기서 주입한다.
"""
import json
import os

import harness_lib as lib

VAULT = lib.VAULT_ROOT
SEARCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vault_search.py")

TEXT = f"""[볼트 읽기 안내] 사용자의 옵시디언 볼트에 과거 트러블슈팅·개념 정리·프로젝트 현황이 있다.
관련 있을 때만 검색한다 — 특히 버그/에러 원인을 찾을 때.

    python3 {SEARCH} "검색어"              # 전체 검색
    python3 {SEARCH} "키워드" -t 트러블슈팅  # 타입 필터 (트러블슈팅·TIL·개념·프로젝트)

이 볼트는 iCloud 경로라 `grep -r`이 조용히 0건을 반환하므로 반드시 위 도구를 쓴다.
0건이어도 정상이니 그냥 진행한다. **볼트에 쓰지 말 것** — 기록할 내용은 결과에 포함해
메인 세션에 반환하면 메인 세션이 기록한다."""


def main():
    data = lib.read_hook_input()
    cwd = data.get("cwd", "")
    if not lib.in_workspace(cwd) or not os.path.isdir(VAULT):
        return
    lib.event("vault-subagent", data.get("session_id", ""),
              data.get("agent_type", ""))
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": TEXT,
        }
    }, ensure_ascii=False))


if __name__ == "__main__":
    lib.run_fail_open(main)
