#!/usr/bin/env python3
"""UserPromptSubmit: 프롬프트에 '!quick' 포함 시 계획 게이트 해제."""
import json

import harness_lib as lib

BYPASS_TOKEN = "!quick"


def bypass_reason(data):
    """우회해야 하면 'user-bypass', 아니면 None."""
    if not lib.in_workspace(data.get("cwd", "")):
        return None
    if BYPASS_TOKEN not in (data.get("prompt") or ""):
        return None
    return "user-bypass"


def main():
    data = lib.read_hook_input()
    reason = bypass_reason(data)
    if not reason:
        return
    lib.set_marker(data.get("session_id", ""), reason)
    lib.event("quick-bypass", data.get("session_id", ""))
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "[workspace harness] 계획 게이트 해제됨(!quick). "
                "이 세션에서는 계획 문서 없이 편집할 수 있습니다."
            ),
        }
    }, ensure_ascii=False))


if __name__ == "__main__":
    lib.run_fail_open(main)
