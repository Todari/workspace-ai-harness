#!/usr/bin/env python3
"""PreToolUse(Bash): 계획 마커 없는 세션의 Bash 기반 소스 쓰기(리다이렉션·sed -i 등)를 차단.

Edit/Write 게이트(plan_gate)의 우회 경로를 막는 방어선. 완벽한 봉쇄가 목적이 아니라
흔한 우회 패턴을 잡고 올바른 경로(계획 작성 또는 !quick)를 안내하는 것이 목적.
"""
import json
import os
import re
import shlex

import harness_lib as lib
import plan_gate

def _resolve(target, cwd):
    target = target.strip("'\"")
    if not target or target.startswith("$") or target.startswith("~"):
        return None  # 변수/홈 확장은 판단 불가 — 보수적으로 허용
    path = target if os.path.isabs(target) else os.path.join(cwd or "", target)
    return os.path.realpath(path)


def _is_gated_target(path):
    if path is None:
        return False
    in_ws = (path == lib.WORKSPACE_ROOT
             or path.startswith(lib.WORKSPACE_ROOT + os.sep))
    if not in_ws:
        return False
    return not plan_gate.is_allowed_path(path)


# heredoc 시작(`<<EOF`, `<<-'EOF'`)만 잡고 herestring(`<<<`)은 제외한다.
HEREDOC_RE = re.compile(
    r"(?<!<)<<-?\s*(?:'([^']*)'|\"([^\"]*)\"|([A-Za-z_][A-Za-z0-9_]*))(?!<)")


def heredoc_delimiter(line):
    """이 줄이 heredoc을 여는가. 열면 종료 구분자를, 아니면 None을 준다."""
    match = HEREDOC_RE.search(line)
    if not match:
        return None
    return match.group(1) or match.group(2) or match.group(3)


def strip_heredoc_bodies(command):
    """heredoc 본문과 종료 구분자를 제거한다 (여는 줄은 남긴다).

    본문은 셸 구문이 아니라 하위 프로세스 입력이다. 이걸 토큰화하면 커밋 메시지의
    `Co-Authored-By: ... <a@b>` 같은 텍스트가 리다이렉션으로 오인된다.
    """
    lines = command.splitlines()
    kept, i = [], 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        delimiter = heredoc_delimiter(line)
        if delimiter is not None:
            i += 1
            while i < len(lines) and lines[i].strip() != delimiter:
                i += 1
        i += 1
    return "\n".join(kept)


def _tokens(command):
    # heredoc 본문은 셸 구문이 아니라 하위 프로세스 입력이다. 본문 속 `>` 등을 검사하지 않는다.
    if "<<" in command:
        command = strip_heredoc_bodies(command)
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return []  # 불완전한 quoting은 판단하지 않고 허용


def find_write_target(command, cwd):
    """게이트 대상 쓰기가 감지되면 설명 문자열, 아니면 None."""
    tokens = _tokens(command)
    for i, tok in enumerate(tokens[:-1]):
        if tok not in (">", ">>"):
            continue
        real = _resolve(tokens[i + 1], cwd)
        if _is_gated_target(real):
            return "리다이렉션 → " + real
    commands = [t for t in tokens if t not in ("|", "||", "&&", ";")]
    if any(commands[i:i + 2] == ["git", "apply"] for i in range(len(commands) - 1)) \
            or "patch" in commands:
        return "patch/git apply"
    for i, tok in enumerate(tokens):
        if os.path.basename(tok) not in ("sed", "perl"):
            continue
        segment = []
        for t in tokens[i + 1:]:
            if t in ("|", "||", "&&", ";"):
                break
            segment.append(t)
        if not any(t == "-i" or t.startswith("-i") for t in segment):
            continue
        for target in segment:
            if target.startswith("-"):
                continue
            real = _resolve(target, cwd)
            if real and os.path.isfile(real) and _is_gated_target(real):
                return "in-place 편집 → " + real
    for i, tok in enumerate(tokens):
        if tok == "tee":
            for t in tokens[i + 1:]:
                if t.startswith("-"):
                    continue
                if t in ("|", "||", "&&", ";"):
                    break
                real = _resolve(t, cwd)
                if _is_gated_target(real):
                    return "tee → " + real
    return None


def decide(data):
    """차단이면 deny JSON dict, 허용이면 None."""
    if not lib.in_workspace(data.get("cwd", "")):
        return None
    if lib.has_marker(data.get("session_id", "")):
        return None
    if data.get("permission_mode") == "plan":
        return None
    command = (data.get("tool_input") or {}).get("command") or ""
    hit = find_write_target(command, data.get("cwd", ""))
    if not hit:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                plan_gate.DENY_MESSAGE
                + " (Bash를 통한 파일 수정도 동일하게 차단됩니다: " + hit + ")"
            ),
        }
    }


def main():
    data = lib.read_hook_input()
    decision = decide(data)
    if decision:
        lib.event("bash-deny", data.get("session_id", ""),
                  ((data.get("tool_input") or {}).get("command") or "")[:120])
        print(json.dumps(decision, ensure_ascii=False))


if __name__ == "__main__":
    lib.run_fail_open(main)
