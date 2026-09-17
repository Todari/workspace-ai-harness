#!/usr/bin/env python3
"""Claude 세션 문맥 감시.

UserPromptSubmit: 트랜스크립트 마지막 요청의 문맥 크기(input+cache)를 읽어 임계값을 넘으면
`/compact`·새 세션을 권고한다. PostToolUse: 도구 출력이 크면 head/tail/grep으로 자르라고 알린다.
2026-09-17 조사: 하네스 주입은 ~1KB인데 요청당 문맥 중앙값 43만·최대 88만 토큰, 이 중 94%가 도구 출력.
"""
import json
import os

import harness_lib as lib

COMPACT_TOKENS = 250_000   # 압축 권고
RESTART_TOKENS = 500_000   # 새 세션 강권
LARGE_OUTPUT_CHARS = 6_000  # 도구 출력 경고
TAIL_BYTES = 512_000        # 트랜스크립트 끝에서 읽는 양
NOTED_TOOLS = ("Bash", "Read", "Grep", "Glob", "WebFetch")


def last_context_tokens(transcript_path):
    """트랜스크립트 끝부분에서 마지막 assistant usage의 전체 문맥 토큰을 돌려준다."""
    if not transcript_path:
        return 0
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            chunk = f.read().decode("utf-8", "ignore")
    except OSError:
        return 0
    for line in reversed(chunk.splitlines()):
        if '"usage"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") != "assistant":
            continue
        usage = (entry.get("message") or {}).get("usage") or {}
        total = sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if total:
            return total
    return 0


def context_advice(tokens):
    if tokens >= RESTART_TOKENS:
        return ("[CONTEXT WATCH] 직전 요청 문맥 %dk 토큰. 매 요청이 이 크기로 과금된다. "
                "지금 답변을 마치면 사용자에게 새 세션 시작을 권하고, 이어갈 상태는 메모리·볼트에 남긴다. "
                "도구 출력은 head/tail/grep으로 자른다." % (tokens // 1000))
    if tokens >= COMPACT_TOKENS:
        return ("[CONTEXT WATCH] 직전 요청 문맥 %dk 토큰. 작업 단위가 끝나면 `/compact`를 권하고, "
                "도구 출력은 head/tail/grep으로 자른다." % (tokens // 1000))
    return ""


def response_text(response):
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        return " ".join(str(response.get(k) or "") for k in
                        ("stdout", "stderr", "output", "content", "text", "file"))
    if isinstance(response, list):
        return " ".join(response_text(item) for item in response)
    return ""


def output_advice(tool_name, response):
    if tool_name not in NOTED_TOOLS:
        return ""
    length = len(response_text(response))
    if length < LARGE_OUTPUT_CHARS:
        return ""
    return ("[CONTEXT WATCH] 도구 출력 %dk자가 문맥에 남았다. 다음부터는 head/tail/grep·범위 읽기로 "
            "필요한 부분만 본다." % (length // 1000))


def main():
    data = lib.read_hook_input()
    if not lib.in_workspace(data.get("cwd", "")):
        return
    event = data.get("hook_event_name", "")
    session = data.get("session_id", "")
    if event == "UserPromptSubmit":
        tokens = last_context_tokens(data.get("transcript_path", ""))
        text = context_advice(tokens)
        if text:
            lib.event("context-watch", session, str(tokens))
            print(lib.hook_output(event, text))
    elif event == "PostToolUse":
        text = output_advice(data.get("tool_name", ""), data.get("tool_response"))
        if text:
            lib.event("context-watch-output", session, data.get("tool_name", ""))
            print(lib.hook_output(event, text))


if __name__ == "__main__":
    lib.run_fail_open(main)
