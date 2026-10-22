#!/usr/bin/env python3
"""Claude용 하네스 명령 자산(/handoff)을 workspace에 동기화한다.

관리 마커가 없는 기존 파일은 덮어쓰지 않는다. 최초 도입 시에는 파일을 직접 검토한 뒤
마커를 추가하거나 대상 파일을 치워야 한다.
"""
import os
import shutil
import sys

import harness_lib as lib

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_ROOT = os.path.join(HERE, "orchestration", "claude")
TARGET_ROOT = os.path.join(lib.WORKSPACE_ROOT, ".claude")
MANAGED_MARKER = "<!-- workspace-harness: managed orchestration -->"
ASSETS = (
    ("commands/handoff.md", "commands/handoff.md"),
)


def asset_paths(source_root=SOURCE_ROOT, target_root=TARGET_ROOT):
    return [(os.path.join(source_root, src), os.path.join(target_root, dst))
            for src, dst in ASSETS]


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def is_synced(source, target):
    try:
        return read_text(source) == read_text(target)
    except OSError:
        return False


def sync_asset(source, target):
    content = read_text(source)
    if MANAGED_MARKER not in content:
        raise ValueError("관리 마커가 없는 소스: %s" % source)
    existed = os.path.exists(target)
    if existed:
        current = read_text(target)
        if current == content:
            return "unchanged"
        if MANAGED_MARKER not in current:
            raise ValueError("관리되지 않는 기존 파일 보존: %s" % target)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(source, target)
    return "updated" if existed else "created"


def main():
    failed = False
    for source, target in asset_paths():
        try:
            state = sync_asset(source, target)
            print("%-9s %s" % (state, target))
        except (OSError, ValueError) as exc:
            failed = True
            print("blocked   %s" % exc, file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
