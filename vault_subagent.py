#!/usr/bin/env python3
"""SubagentStart: 서브에이전트에 볼트 읽기 인터페이스를 알린다 (쓰기는 금지).

메인 세션은 SessionStart 훅으로 볼트를 인지하지만 서브에이전트는 그 훅이 돌지 않는다.
서브에이전트가 볼트를 직접 grep하면 이 iCloud 경로에서 조용히 0건이 나오므로,
표준 검색 도구의 존재와 읽기 전용 규칙을 여기서 짧게 주입한다(서브에이전트마다 붙는 비용).
"""
import os

import harness_lib as lib

VAULT = lib.VAULT_ROOT
SEARCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vault_search.py")

TEXT = (
    "[볼트] 과거 트러블슈팅·개념·프로젝트 노트 검색: "
    f"python3 {SEARCH} \"키워드\" [-t 트러블슈팅|TIL|개념|프로젝트]\n"
    "버그 원인 추적처럼 관련 있을 때만 검색한다(0건이면 정상). iCloud 경로라 grep -r은 0건을 "
    "반환하니 쓰지 말 것. 볼트에 쓰지 말 것 — 기록할 내용은 결과에 담아 메인 세션에 반환한다."
)


def main():
    data = lib.read_hook_input()
    cwd = data.get("cwd", "")
    if not lib.in_workspace(cwd) or not os.path.isdir(VAULT):
        return
    lib.event("vault-subagent", data.get("session_id", ""),
              data.get("agent_type", ""))
    print(lib.hook_output("SubagentStart", TEXT))


if __name__ == "__main__":
    lib.run_fail_open(main)
