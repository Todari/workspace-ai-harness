#!/usr/bin/env python3
"""개발용: 훅 입력 JSON을 그대로 ~/.claude/cache/harness-debug.jsonl에 덤프.

스키마 조사가 필요할 때만 임시로 등록해 쓰고, 조사가 끝나면 등록을 해제한다.
"""
import json
import os

import harness_lib as lib

DEBUG_PATH = os.path.join(lib.CACHE_DIR, "harness-debug.jsonl")


def main():
    data = lib.read_hook_input()
    os.makedirs(lib.CACHE_DIR, exist_ok=True)
    with open(DEBUG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    lib.run_fail_open(main)
